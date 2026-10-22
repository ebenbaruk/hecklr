"""``hecklr calibrate``: the A/A self-test (SC5) and judge agreement with human labels (SC7)."""

import os
import random
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import anyio
import click
import pytest
from typer.testing import CliRunner

import hecklr.commands._common
from hecklr import exit_codes, voice_calibration
from hecklr.cli import app
from hecklr.commands._common import CliOverrides
from hecklr.testing import scripted_models
from hecklr.voice_calibration import TimingCheck
from hecklr_core import ResultStore
from hecklr_schemas import HumanLabel, LabelId, Run, new_id

Edit = Callable[[Path, str, str], None]
StoreRun = Callable[..., Run]

runner = CliRunner()
SCENARIOS = {f"s{i:02d}": 0.8 for i in range(30)}

# The world sandbox and the starter agent listen on 127.0.0.1 ephemeral ports.
pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]


@pytest.fixture(autouse=True)
def fast_stats(config_path: Path, edit: Edit) -> None:
    """Fewer bootstrap resamples and a fixed seed keep the statistics fast and deterministic."""
    edit(
        config_path,
        "stats: { alpha: 0.05, min_effect: 0.03 }",
        "stats: { alpha: 0.05, min_effect: 0.03, bootstrap_resamples: 200, seed: 3 }",
    )


@pytest.fixture(autouse=True)
def wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """The judge table has eight columns; keep each cell on one line."""
    monkeypatch.setattr(hecklr.commands._common.stdout, "width", 200)


def invoke(config_path: Path, *args: str) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["calibrate", *args, "--config", str(config_path)],
        obj=CliOverrides(scripted_models()),
    )
    return result.exit_code, " ".join(click.unstyle(result.output).replace("│", " ").split())


# --- A/A ----------------------------------------------------------------------------------------


def test_sc5_aa_runs_the_same_agent_twice_with_different_seeds(
    config_path: Path, runs_dir: Path
) -> None:
    code, output = invoke(config_path, "--aa", "--trials", "1", "--replicates", "20")

    assert code == exit_codes.OK, output
    assert "A/A comparison: passed, no regression flagged" in output
    assert re.search(r"Simulated A/A false-alarm rate \(5 scenarios x 1 trials, 20", output)
    assert "Power to detect a 10-point pass-rate drop" in output
    store = ResultStore(runs_dir)
    first, second = store.list_runs()
    assert second.seed == first.seed + 1


def test_sc5_aa_on_two_stored_runs_of_the_same_agent(config_path: Path, store: StoreRun) -> None:
    a = store(SCENARIOS, order=0, seed=1)
    b = store(SCENARIOS, order=1, seed=2)

    code, output = invoke(config_path, "--aa", "--runs", a.id, "--runs", b.id, "--replicates", "30")

    assert code == exit_codes.OK, output
    assert "A/A comparison: passed" in output
    assert "target <= 5% ok" in output


def test_aa_fails_when_the_runs_differ(config_path: Path, store: StoreRun) -> None:
    a = store(dict.fromkeys(SCENARIOS, 1.0), order=0)
    b = store({s: 0.0 if i < 15 else 1.0 for i, s in enumerate(SCENARIOS)}, order=1)

    code, output = invoke(config_path, "--aa", "--runs", a.id, "--runs", b.id, "--replicates", "0")

    assert code == exit_codes.CHECK_FAILED, output
    assert "A/A comparison: FAILED, a regression was flagged" in output
    assert "Simulated" not in output


def test_aa_needs_exactly_two_runs(config_path: Path, store: StoreRun) -> None:
    a = store(SCENARIOS)

    code, output = invoke(config_path, "--aa", "--runs", a.id)

    assert code == exit_codes.CONFIG_ERROR
    assert "exactly two runs" in output


def test_sc5_aa_refuses_the_same_run_twice(config_path: Path, store: StoreRun) -> None:
    a = store(SCENARIOS)

    code, output = invoke(config_path, "--aa", "--runs", a.id, "--runs", a.id)

    assert code == exit_codes.CONFIG_ERROR
    assert f"names the same run {a.id} twice" in output
    assert "A/A comparison" not in output


def test_aa_on_runs_that_cannot_be_compared_is_a_harness_error(
    config_path: Path, store: StoreRun
) -> None:
    a = store({"s00": 1.0}, order=0)
    b = store({"s99": 1.0}, order=1)

    code, output = invoke(config_path, "--aa", "--runs", a.id, "--runs", b.id)

    assert code == exit_codes.HARNESS_ERROR
    assert "share no analyzable scenario" in output


# --- judges -------------------------------------------------------------------------------------


def _labels(path: Path, runs_dir: Path, run: Run, *, agree: float) -> Path:
    """Label each judge grade of ``run``; a labeler agrees with the judge with ``agree``."""

    async def grades() -> list[tuple[str, str, bool, str]]:
        async with ResultStore(runs_dir) as store:
            results = await store.load_run(run.id)
        return [(g.trial_id, g.id, bool(g.passed), g.grader_version) for g in results.grades]

    rng = random.Random(5)
    lines = [
        HumanLabel(
            id=LabelId(new_id()),
            trial_id=trial_id,  # type: ignore[arg-type]  # a stored TrialId, read back as str
            grade_id=grade_id,  # type: ignore[arg-type]  # a stored GradeId, read back as str
            grader="judge:resolved",
            grader_version=version,
            passed=passed if rng.random() < agree else not passed,
            labeler="qa",
            created_at=datetime(2026, 10, 21, tzinfo=UTC),
        ).model_dump_json()
        for trial_id, grade_id, passed, version in anyio.run(grades)
    ]
    path.write_text("\n".join(lines) + "\n\n", encoding="utf-8")
    return path


def test_sc7_judges_agreeing_with_humans_are_trusted(
    config_path: Path, store: StoreRun, runs_dir: Path, tmp_path: Path
) -> None:
    run = store(SCENARIOS, grader="judge:resolved")
    labels = _labels(tmp_path / "labels.jsonl", runs_dir, run, agree=1.0)

    code, output = invoke(config_path, "--judges", "--labels", str(labels))

    assert code == exit_codes.OK, output
    assert "Judge agreement with human labels" in output
    assert "judge:resolved" in output
    assert "fake/judge" in output
    assert "1.00 [1.00, 1.00]" in output  # kappa
    assert " yes" in output


def test_sc7_a_judge_humans_disagree_with_is_untrusted(
    config_path: Path, store: StoreRun, runs_dir: Path, tmp_path: Path
) -> None:
    run = store(SCENARIOS, grader="judge:resolved")
    labels = _labels(tmp_path / "labels.jsonl", runs_dir, run, agree=0.5)

    code, output = invoke(config_path, "--judges", "--labels", str(labels), "--run", run.id)

    assert code == exit_codes.CHECK_FAILED, output
    assert "NO (kappa < 0.70)" in output


def test_judges_need_matching_labels(
    config_path: Path, store: StoreRun, runs_dir: Path, tmp_path: Path
) -> None:
    labeled = store(SCENARIOS, grader="judge:resolved", order=0)
    labels = _labels(tmp_path / "labels.jsonl", runs_dir, labeled, agree=1.0)
    store(SCENARIOS, grader="tool_called", order=1)

    code, output = invoke(config_path, "--judges", "--labels", str(labels))

    assert code == exit_codes.CONFIG_ERROR
    assert "no label in" in output


def test_judges_reject_a_malformed_label_file(config_path: Path, tmp_path: Path) -> None:
    labels = tmp_path / "labels.jsonl"
    labels.write_text('{"passed": true}\n', encoding="utf-8")

    code, output = invoke(config_path, "--judges", "--labels", str(labels))

    assert code == exit_codes.CONFIG_ERROR
    assert "labels.jsonl:1: not a human label" in output


def test_judges_need_a_labels_file(config_path: Path) -> None:
    code, output = invoke(config_path, "--judges")

    assert code == exit_codes.CONFIG_ERROR
    assert "--judges needs --labels FILE" in output


# --- the rest -----------------------------------------------------------------------------------


def test_calibrate_needs_a_self_test(config_path: Path) -> None:
    code, output = invoke(config_path)

    assert code == exit_codes.CONFIG_ERROR
    assert "choose a self-test" in output


@pytest.mark.slow
def test_sc4_calibrate_voice_loopback_measures_within_20ms(tmp_path: Path) -> None:
    # The installed console script, in a subprocess: the voice engine's DSP worker processes
    # must start under the real entry point (they failed to import it, see hecklr.cli.main).
    script = Path(sys.executable).parent / "hecklr"
    done = subprocess.run(  # noqa: S603 -- fixed arguments, no shell
        [str(script), "calibrate", "--voice-loopback"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        cwd=tmp_path,
        env={**os.environ, "COLUMNS": "200", "NO_COLOR": "1"},
    )
    code = done.returncode
    output = " ".join((done.stdout + done.stderr).replace("│", " ").split())

    # Ten real-time calls measure this machine as much as the harness: next to a parallel test
    # run's other workers, a loaded CPU can push one reply past 20 ms, and the command rightly
    # says so (exit 1). The verdict is asserted on an otherwise idle machine, as the voice
    # contract's SC4 cases are; the command must run to a full table everywhere.
    if os.environ.get("PYTEST_XDIST_WORKER"):
        assert code in (exit_codes.OK, exit_codes.CHECK_FAILED), output
    else:
        assert code == exit_codes.OK, output
        assert "Voice loopback: passed" in output
    for metric, injected in [
        ("response_latency", 300), ("response_latency", 600), ("response_latency", 900),
        ("barge_in_reaction", 150), ("barge_in_reaction", 300),
    ]:  # fmt: skip
        row = re.search(rf"{metric} {injected} (\d+) [+-][\d.]+ \[[+-][\d.]+, [+-][\d.]+\]", output)
        assert row, (metric, injected, output)
        assert int(row.group(1)) >= 2


def test_sc4_a_timing_error_above_20ms_fails_the_calibration(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def off_by_25(seed: int) -> list[TimingCheck]:
        return [
            TimingCheck("response_latency", 600, (3.0, -2.0)),
            TimingCheck("barge_in_reaction", 150, (25.0, 1.0)),
            TimingCheck("barge_in_reaction", 300, ()),
        ]

    monkeypatch.setattr(voice_calibration, "run_voice_loopback", off_by_25)
    code, output = invoke(config_path, "--voice-loopback")

    assert code == exit_codes.CHECK_FAILED
    assert "FAILED, timing off by more than 20 ms" in output
    assert "no measurement" in output
    assert "barge_in_reaction 150 2 +13.0 [" in output


def test_sc4_timing_errors_within_20ms_pass_the_calibration(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def on_time(seed: int) -> list[TimingCheck]:
        return [TimingCheck("response_latency", 300, (2.0, -1.0, 3.0))]

    monkeypatch.setattr(voice_calibration, "run_voice_loopback", on_time)
    code, output = invoke(config_path, "--voice-loopback")

    assert code == exit_codes.OK
    assert "response_latency 300 3 +1.3 [" in output
    assert "Voice loopback: passed" in output


def test_voice_loopback_without_the_engine_names_the_extra(
    config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "hecklr_voice", None)
    code, output = invoke(config_path, "--voice-loopback")

    assert code == exit_codes.CONFIG_ERROR
    assert "pip install 'hecklr[voice]'" in output
