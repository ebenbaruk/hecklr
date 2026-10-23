"""The benchmark's parts on their own: placeholders, processes, metrics, reproduction, the site."""

import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import JsonValue

from hecklr.commands.bench.process import StackError, agent_process, read_spend
from hecklr.commands.bench.result import (
    Environment,
    Spend,
    StackResult,
    check_reproduction,
    judge_scores,
    missing_metrics,
)
from hecklr.commands.bench.runner import patched_environ, suite_digest
from hecklr.commands.bench.site import build_site, load_results
from hecklr.commands.bench.spec import (
    Ready,
    fill,
    fill_json,
    load_manifest,
    load_stacks,
)
from hecklr_core import ConfigError, RunResults
from hecklr_report import TrialCounts
from hecklr_schemas import (
    AgentRef,
    Budget,
    Channel,
    Estimate,
    Grade,
    GradeId,
    IntervalMethod,
    Run,
    RunId,
    RunStatus,
    Scenario,
    Trial,
    TrialId,
    WorldSnapshot,
    new_id,
)

pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]


# --- placeholders and files --------------------------------------------------------------------


def test_fill_replaces_every_placeholder_inside_json() -> None:
    values = {"port": "8123", "python": "/py"}
    adapter: JsonValue = {
        "url": "ws://127.0.0.1:{port}/",
        "args": ["{python}", 3, None],
        "nested": {"a": "{port}"},
    }

    assert fill_json(adapter, values) == {
        "url": "ws://127.0.0.1:8123/",
        "args": ["/py", 3, None],
        "nested": {"a": "8123"},
    }


def test_fill_rejects_an_unknown_placeholder() -> None:
    with pytest.raises(KeyError, match="prot"):
        fill("--port {prot}", {"port": "1"})


def test_a_stack_file_must_be_named_after_its_id(bench_dir: Path) -> None:
    stacks = bench_dir / "stacks"
    (stacks / "acme-rules.yaml").rename(stacks / "other.yaml")

    with pytest.raises(ConfigError, match=r"file name must be acme-rules\.yaml"):
        load_stacks(bench_dir)


def test_an_invalid_stack_names_its_file_and_line(bench_dir: Path) -> None:
    path = bench_dir / "stacks" / "acme-rules.yaml"
    path.write_text(path.read_text("utf-8").replace("track: chat", "track: fax"), "utf-8")

    with pytest.raises(ConfigError, match=r"acme-rules.yaml:3"):
        load_stacks(bench_dir)


def test_the_real_benchmark_files_load() -> None:
    bench = Path(__file__).resolve().parents[4] / "bench"

    manifest = load_manifest(bench)
    stacks = load_stacks(bench)

    assert {"chat", "voice", "calibration"} <= manifest.tracks.keys()
    assert len(stacks) >= 6
    assert all(stack.track in manifest.tracks for stack in stacks.values())


def test_suite_digest_changes_with_any_suite_file(bench_dir: Path) -> None:
    manifest = load_manifest(bench_dir)
    before = suite_digest(bench_dir, manifest)
    (bench_dir / "suite" / "personas" / "extra.yaml").write_text("id: x\n", "utf-8")

    assert suite_digest(bench_dir, manifest) != before


def test_patched_environ_restores_what_was_there(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HECKLR_BENCH_A", "old")
    monkeypatch.delenv("HECKLR_BENCH_B", raising=False)

    with patched_environ({"HECKLR_BENCH_A": "new", "HECKLR_BENCH_B": "set"}):
        assert (os.environ["HECKLR_BENCH_A"], os.environ["HECKLR_BENCH_B"]) == ("new", "set")

    assert os.environ["HECKLR_BENCH_A"] == "old"
    assert "HECKLR_BENCH_B" not in os.environ


# --- the agent process -------------------------------------------------------------------------


def test_an_agent_is_ready_when_its_log_shows_the_line(tmp_path: Path) -> None:
    script = "import time; print('listening on ws://x', flush=True); time.sleep(60)"

    with agent_process(
        [sys.executable, "-c", script],
        env={},
        ready=Ready(line=r"listening on", timeout_s=30),
        cwd=tmp_path,
        log_path=tmp_path / "agent.log",
    ) as process:
        assert process.poll() is None

    assert process.returncode is not None  # stopped when the block ended


def test_an_agent_that_never_gets_ready_is_stopped(tmp_path: Path) -> None:
    script = "import time; time.sleep(60)"

    with (
        pytest.raises(StackError, match="not ready within 1 s"),
        agent_process(
            [sys.executable, "-c", script],
            env={},
            ready=Ready(line="never", timeout_s=1),
            cwd=tmp_path,
            log_path=tmp_path / "agent.log",
        ),
    ):
        pytest.fail("the block must not run")


def test_reading_spend_from_a_silent_agent_fails_loudly() -> None:
    with pytest.raises(StackError, match="cannot read the agent's spend"):
        read_spend("http://127.0.0.1:9/healthz")


# --- metrics -----------------------------------------------------------------------------------

_SCENARIO = Scenario.model_validate(
    {
        "id": "s1",
        "title": "t",
        "channels": ["chat"],
        "persona": "p",
        "world": "w",
        "goal": "g",
        "success": [
            {"type": "state", "expr": "True"},
            {"type": "judge", "rubric": "resolved"},
        ],
    }
)


def _results(statuses: list[str], judged: list[bool | None]) -> RunResults:
    run = Run(
        id=RunId(new_id()),
        created_at=datetime(2026, 10, 23, tzinfo=UTC),
        agent=AgentRef(name="a"),
        config_hash="h",
        seed=1,
        matrix=[],
        budget=Budget(),
        status=RunStatus.COMPLETED,
    )
    trials, grades = [], []
    snapshot = WorldSnapshot(world_id="w", taken_at_ms=0, collections={})
    for index, (status, passed) in enumerate(zip(statuses, judged, strict=True)):
        trial = Trial(
            id=TrialId(new_id()),
            run_id=run.id,
            scenario_id="s1",
            persona_id="p",
            channel=Channel.CHAT,
            index=index,
            seed=index,
            status=status,  # type: ignore[arg-type]  # test data: a valid TrialStatus literal
            started_at=run.created_at,
            duration_ms=10,
            cost_usd=Decimal("0.001"),
            world_before=snapshot,
            world_after=snapshot,
        )
        trials.append(trial)
        if passed is not None:
            grades.append(
                Grade(
                    id=GradeId(new_id()),
                    trial_id=trial.id,
                    grader="judge:resolved",
                    grader_version="1",
                    criterion_index=1,
                    value=passed,
                    passed=passed,
                )
            )
    return RunResults(run=run, trials=trials, events=[], grades=grades)


def test_judge_scores_count_failed_conversations_and_skip_unjudged_ones() -> None:
    results = _results(
        ["ok", "ok", "agent_error", "harness_error", "ok"], [True, False, True, True, None]
    )

    scores = judge_scores(results, {"s1": _SCENARIO}, seed=1, resamples=200)

    # ok+pass, ok+fail, agent error (fails though the judge passed it); the harness error and
    # the unjudged trial are left out.
    assert scores["judge:resolved"].value == pytest.approx(1 / 3)
    assert scores["judge:resolved"].ci_low <= 1 / 3 <= scores["judge:resolved"].ci_high


def _estimate(value: float, low: float, high: float) -> Estimate:
    return Estimate(value=value, ci_low=low, ci_high=high, n=5, method=IntervalMethod.BOOTSTRAP)


def _row(metrics: dict[str, Estimate], version: str = "1.0") -> StackResult:
    stack = load_stacks(Path(__file__).resolve().parents[4] / "bench")["acme-chat-rules"]
    return StackResult(
        benchmark_version=version,
        suite_digest="d",
        stack=stack,
        run_id=new_id(),
        created_at=datetime(2026, 10, 23, tzinfo=UTC),
        seed=1,
        trials_per_scenario=2,
        scenarios=8,
        models={},
        environment=Environment(
            hecklr="0",
            python="3.12",
            platform="p",
            machine="m",
            cpus=1,
            git_sha=None,
            packages={},
            load_average=(1.0, 1.0),
        ),
        counts=TrialCounts(
            total=1,
            ok=1,
            agent_error=0,
            timeout=0,
            harness_error=0,
            undecided=0,
            grade_errors=0,
            analyzed=1,
            contradiction_rate=None,
        ),
        metrics=metrics,
        spend=Spend(hecklr_usd=Decimal(1), agent_usd=Decimal(2)),
        duration_s=1.0,
    )


def test_reproduction_is_judged_by_overlapping_intervals() -> None:
    published = _row({
        "inside": _estimate(10, 8, 12),
        "overlapping": _estimate(10, 8, 12),
        "apart": _estimate(10, 8, 12),
        "only_published": _estimate(1, 0, 2),
    })  # fmt: skip
    reproduced = _row({
        "inside": _estimate(11, 9, 13),
        "overlapping": _estimate(13, 11.5, 15),
        "apart": _estimate(20, 18, 22),
    })  # fmt: skip

    checks = {c.metric: c for c in check_reproduction(published, reproduced)}

    assert (checks["inside"].within, checks["inside"].consistent) == (True, True)
    assert (checks["overlapping"].within, checks["overlapping"].consistent) == (False, True)
    assert (checks["apart"].within, checks["apart"].consistent) == (False, False)
    assert missing_metrics(published, reproduced) == ["only_published"]
    assert published.spend.total_usd == 3


# --- the site ----------------------------------------------------------------------------------


def test_the_site_leaves_out_rows_of_another_benchmark_version(tmp_path: Path) -> None:
    bench = Path(__file__).resolve().parents[4] / "bench"
    results = tmp_path / "results"
    results.mkdir()
    (results / "old.json").write_text(_row({}, version="0.9").model_dump_json(), "utf-8")
    (results / "new.json").write_text(_row({"x": _estimate(1, 0, 2)}).model_dump_json(), "utf-8")
    manifest = load_manifest(bench)

    index = build_site(
        manifest, load_results(results), source=bench / "site" / "src", out=tmp_path / "dist"
    )

    data = (index.parent / "data.js").read_text("utf-8")
    assert data.count('"benchmark_version": "1.0"') == 2  # the site's own, and the new row's
    assert '"0.9"' not in data


def test_a_malformed_result_file_is_named(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{}", "utf-8")

    with pytest.raises(ConfigError, match=r"bad.json: not a benchmark result"):
        load_results(tmp_path)
