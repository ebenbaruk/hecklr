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
from hecklr import exit_codes, judge_calibration, voice_calibration
from hecklr.cli import app
from hecklr.commands._common import CliOverrides
from hecklr.testing import scripted_models
from hecklr.voice_calibration import TimingCheck
from hecklr_core import ResultStore
from hecklr_llm import FakeLLM
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


def test_sc7_judges_ignore_labels_of_other_runs_instead_of_crashing(
    config_path: Path, store: StoreRun, runs_dir: Path, tmp_path: Path
) -> None:
    # Same rubric version, labels of run A, grades of run B: nothing joins (was a ValueError).
    labeled = store(SCENARIOS, grader="judge:resolved", order=0)
    other = store(SCENARIOS, grader="judge:resolved", order=1)
    labels = _labels(tmp_path / "labels.jsonl", runs_dir, labeled, agree=1.0)

    code, output = invoke(config_path, "--judges", "--labels", str(labels), "--run", other.id)

    assert code == exit_codes.CONFIG_ERROR, output
    assert "no label in" in output


def test_sc7_judges_keep_only_the_chosen_labelers(
    config_path: Path, store: StoreRun, runs_dir: Path, tmp_path: Path
) -> None:
    run = store(SCENARIOS, grader="judge:resolved")
    labels = _labels(tmp_path / "labels.jsonl", runs_dir, run, agree=1.0)

    code, output = invoke(config_path, "--judges", "--labels", str(labels), "--labeler", "human:")

    assert code == exit_codes.CONFIG_ERROR, output
    assert "no label in" in output


# --- judges on a labeled transcript set (--set) ---------------------------------------------------

SET_WORLD = """\
id: tiny
collections:
  customers:
    key: customer_id
    records:
      - { customer_id: C-1, name: Ada, postcode: "75001" }
tools:
  - name: verify_customer
    description: Verify a caller.
    params:
      type: object
      required: [customer_id, postcode]
      properties: { customer_id: { type: string }, postcode: { type: string } }
    op: { kind: find, collection: customers }
"""

_POLITE = """\
- id: polite-{i}
  channel: chat
  goal: Ask for help.
  rubrics: {{tone: {{}}, resolved: {{}}}}
  turns:
    - customer: Can you help? C-1, 75001.
    - tool: verify_customer
      args: {{customer_id: C-1, postcode: "75001"}}
    - agent: Of course, Ada. What do you need?
"""
_RUDE = """\
- id: rude-{i}
  channel: chat
  goal: Ask for help.
  rubrics: {{tone: {{}}, resolved: {{}}}}
  turns:
    - customer: Can you help?
    - agent: Figure it out yourself.
"""
SET_PER_CLASS = 10
"""Polite (pass) and rude (fail) cases, each judged on two rubrics: the fewest that a rubric can be
trusted from (``MIN_LABELS_PER_CLASS`` of each class, issue #100)."""
SET_PAIRS = 4 * SET_PER_CLASS
SET_CASES = "".join(
    template.format(i=i) for template in (_POLITE, _RUDE) for i in range(SET_PER_CLASS)
)


def _labeled_set(root: Path, cases: str = SET_CASES, *, labeler: str = "human:qa") -> Path:
    (root / "cases").mkdir(parents=True)
    (root / "world.yaml").write_text(SET_WORLD, encoding="utf-8")
    (root / "cases" / "cases.yaml").write_text(cases, encoding="utf-8")
    (root / "set.yaml").write_text(
        "version: 1\nworld: world.yaml\ncases: cases/*.yaml\nlabels: labels.jsonl\n",
        encoding="utf-8",
    )
    (root / "labels.jsonl").touch()
    calibration_set = judge_calibration.load_set(root)
    when = datetime(2026, 10, 23, tzinfo=UTC)
    for case, rubric in calibration_set.items():
        passed = case.id.startswith("polite")
        label = judge_calibration.make_label(
            case, rubric, passed=passed, labeler=labeler, note=None, created_at=when
        )
        judge_calibration.append_label(calibration_set.labels_path, label)
    return root


def _invoke_set(config_path: Path, judge: FakeLLM, *args: str) -> tuple[int, str]:
    result = runner.invoke(
        app,
        ["calibrate", "--judges", *args, "--config", str(config_path)],
        obj=CliOverrides({"fake": judge}),
    )
    return result.exit_code, " ".join(click.unstyle(result.output).replace("│", " ").split())


def _judge(rule: Callable[[str], bool]) -> FakeLLM:
    """A judge passing a conversation when ``rule(prompt)`` holds."""
    return FakeLLM(
        responder=lambda request: {
            "verdict": "pass" if rule(request.messages[0].content) else "fail",
            "rationale": "Scripted.",
            "evidence": [],
        }
    )


def _agrees(prompt: str) -> bool:
    return "yourself" not in prompt


def test_sc7_calibrate_judges_set_judges_the_transcripts_and_trusts_agreement(
    config_path: Path, tmp_path: Path
) -> None:
    root = _labeled_set(tmp_path / "set")

    code, output = _invoke_set(config_path, _judge(_agrees), "--set", str(root))

    assert code == exit_codes.OK, output
    assert f"Judging {SET_PAIRS} labeled (conversation, rubric) pairs" in output
    assert "judge:tone" in output
    assert "judge:resolved" in output
    assert "Judge spend: $0.0000" in output
    assert "NO (kappa < 0.70)" not in output
    assert "note:" not in output  # all labels are human


def test_sc7_calibrate_judges_set_flags_a_disagreeing_judge(
    config_path: Path, tmp_path: Path
) -> None:
    root = _labeled_set(tmp_path / "set")

    code, output = _invoke_set(config_path, _judge(lambda prompt: True), "--set", str(root))

    assert code == exit_codes.CHECK_FAILED, output
    assert "judge:tone" in output
    assert "NO (kappa < 0.70)" in output


def test_sc7_calibrate_judges_set_says_ai_drafts_are_not_human_labels(
    config_path: Path, tmp_path: Path
) -> None:
    root = _labeled_set(tmp_path / "set", labeler="claude-draft")

    code, output = _invoke_set(config_path, _judge(_agrees), "--set", str(root))

    assert code == exit_codes.OK, output
    assert (
        f"{SET_PAIRS} of {SET_PAIRS} labels are not from 'human:' labelers (claude-draft)" in output
    )

    judge = _judge(_agrees)
    code, output = _invoke_set(config_path, judge, "--set", str(root), "--labeler", "human:")

    # No human label: refused before a single (paid) judge call.
    assert code == exit_codes.CONFIG_ERROR, output
    assert "no label by human: is of a current rubric version" in output
    assert judge.calls == []


def _age_labels(labels: Path, grader: str) -> None:
    """Pretend ``grader``'s rubric changed since it was labeled: its labels pin an old version."""
    rows = [HumanLabel.model_validate_json(line) for line in labels.read_text().splitlines()]
    labels.write_text(
        "".join(
            (
                row.model_copy(update={"grader_version": "old"}) if row.grader == grader else row
            ).model_dump_json()
            + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def test_sc7_calibrate_judges_set_fails_a_rubric_labeled_only_at_an_old_version(
    config_path: Path, tmp_path: Path
) -> None:
    root = _labeled_set(tmp_path / "set")
    _age_labels(root / "labels.jsonl", "judge:resolved")
    judge = _judge(_agrees)

    code, output = _invoke_set(config_path, judge, "--set", str(root))

    assert code == exit_codes.CHECK_FAILED, output
    assert f"Judging {SET_PAIRS // 2} labeled (conversation, rubric) pairs" in output
    assert len(judge.calls) == SET_PAIRS // 2  # the stale pairs are not judged
    assert "judge:resolved: not calibrated, no label of its current rubric version" in output
    assert "calibration/judges/GUIDE.md" in output


def test_sc7_calibrate_judges_set_with_only_old_labels_is_refused(
    config_path: Path, tmp_path: Path
) -> None:
    root = _labeled_set(tmp_path / "set")
    for grader in ("judge:resolved", "judge:tone"):
        _age_labels(root / "labels.jsonl", grader)

    code, output = _invoke_set(config_path, _judge(_agrees), "--set", str(root))

    assert code == exit_codes.CONFIG_ERROR, output
    assert "no label is of a current rubric version" in output


def test_sc7_calibrate_judges_set_reports_a_judge_that_never_answers_usably(
    config_path: Path, tmp_path: Path
) -> None:
    root = _labeled_set(tmp_path / "set")

    code, output = _invoke_set(
        config_path, FakeLLM(responder=lambda request: "not json"), "--set", str(root)
    )

    assert code == exit_codes.CHECK_FAILED, output
    assert "judge:tone: not calibrated, every judge answer was unusable" in output
    assert f"judge:tone: {SET_PAIRS // 2} judge answer(s) unusable, left out" in output


def test_sc7_calibrate_judges_set_takes_an_explicit_labels_file(
    config_path: Path, tmp_path: Path
) -> None:
    root = _labeled_set(tmp_path / "set")
    elsewhere = tmp_path / "mine.jsonl"
    elsewhere.write_text((root / "labels.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
    (root / "labels.jsonl").write_text("", encoding="utf-8")

    code, output = _invoke_set(
        config_path, _judge(_agrees), "--set", str(root), "--labels", str(elsewhere)
    )

    assert code == exit_codes.OK, output


def test_sc7_calibrate_judges_set_rejects_a_malformed_set(
    config_path: Path, tmp_path: Path
) -> None:
    root = tmp_path / "set"
    (root / "cases").mkdir(parents=True)
    (root / "world.yaml").write_text(SET_WORLD, encoding="utf-8")
    (root / "cases" / "cases.yaml").write_text("- {id: x}\n", encoding="utf-8")
    (root / "labels.jsonl").touch()
    (root / "set.yaml").write_text(
        "version: 1\nworld: world.yaml\ncases: cases/*.yaml\nlabels: labels.jsonl\n",
        encoding="utf-8",
    )

    code, output = _invoke_set(config_path, _judge(_agrees), "--set", str(root))

    assert code == exit_codes.CONFIG_ERROR, output
    assert "case 0" in output


def test_sc7_calibrate_judges_set_rejects_a_case_the_world_refuses(
    config_path: Path, tmp_path: Path
) -> None:
    root = _labeled_set(tmp_path / "set")
    cases = root / "cases" / "cases.yaml"
    cases.write_text(
        SET_CASES.replace('postcode: "75001"}', 'postcode: "99999"}'), encoding="utf-8"
    )

    code, output = _invoke_set(config_path, _judge(_agrees), "--set", str(root))

    assert code == exit_codes.CONFIG_ERROR, output
    assert "verify_customer was refused" in output


def test_sc7_calibrate_judges_set_rejects_a_malformed_labels_file(
    config_path: Path, tmp_path: Path
) -> None:
    root = _labeled_set(tmp_path / "set")
    (root / "labels.jsonl").write_text("{}\n", encoding="utf-8")

    code, output = _invoke_set(config_path, _judge(_agrees), "--set", str(root))

    assert code == exit_codes.CONFIG_ERROR, output
    assert "labels.jsonl:1: not a human label" in output


def test_sc7_calibrate_judges_set_and_run_are_exclusive(config_path: Path, tmp_path: Path) -> None:
    root = _labeled_set(tmp_path / "set")

    code, output = _invoke_set(config_path, _judge(_agrees), "--set", str(root), "--run", "latest")

    assert code == exit_codes.CONFIG_ERROR
    assert "--set judges its own transcripts; drop --run" in output


@pytest.mark.parametrize("args", [["--set", "SET"], ["--labeler", "human:"]])
def test_sc7_set_and_labeler_need_judges(
    config_path: Path, tmp_path: Path, args: list[str]
) -> None:
    root = _labeled_set(tmp_path / "set")

    code, output = invoke(config_path, "--aa", *[str(root) if a == "SET" else a for a in args])

    assert code == exit_codes.CONFIG_ERROR
    assert "--set and --labeler only apply to --judges" in output


# --- the rest -----------------------------------------------------------------------------------


def test_calibrate_needs_a_self_test(config_path: Path) -> None:
    code, output = invoke(config_path)

    assert code == exit_codes.CONFIG_ERROR
    assert "choose a self-test" in output


@pytest.mark.slow
@pytest.mark.sc4_timing
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
        # CI's FORCE_COLOR would beat NO_COLOR and put ANSI codes into the verdict line.
        env={
            **{k: v for k, v in os.environ.items() if k != "FORCE_COLOR"},
            "COLUMNS": "200",
            "NO_COLOR": "1",
        },
    )
    code = done.returncode
    output = " ".join((done.stdout + done.stderr).replace("│", " ").split())

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


def test_issue100_a_rubber_stamp_judge_on_all_pass_labels_fails_sc7(
    config_path: Path, tmp_path: Path
) -> None:
    polite_only = "".join(_POLITE.format(i=i) for i in range(SET_PER_CLASS))
    root = _labeled_set(tmp_path / "set", polite_only)

    code, output = _invoke_set(config_path, _judge(lambda prompt: True), "--set", str(root))

    assert code == exit_codes.CHECK_FAILED, output
    assert "1.00 [1.00, 1.00]" in output  # agreement never tested on a fail
    assert "NO (needs 10+ human labels of each class, has 10 pass and 0 fail)" in output
