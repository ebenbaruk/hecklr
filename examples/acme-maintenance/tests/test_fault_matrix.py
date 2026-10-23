"""scripts/fault-matrix: grader attribution, the SC8 pass rule, budgets and a real free-tier run."""

import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from acme_agent import PROJECT_ROOT
from acme_testing import load_scenarios

from hecklr_report import FailureCause, TrialRow
from hecklr_schemas import (
    Channel,
    Estimate,
    IntervalMethod,
    RunId,
    Scenario,
    StatsConfig,
    TrialId,
    TrialStatus,
    Verdict,
    new_id,
)

sys.path.insert(0, str(PROJECT_ROOT.parents[1] / "scripts" / "fault-matrix"))
import fault_matrix as fm

STATS = StatsConfig(bootstrap_resamples=500)
AGENT = "openai/gpt-6-luna"


def scenario(sid: str, *criteria: Mapping[str, object]) -> Scenario:
    return Scenario.model_validate(
        {
            "id": sid,
            "title": sid,
            "channels": ["chat"],
            "persona": "p",
            "world": "w",
            "goal": "g",
            "success": list(criteria),
        }
    )


RESOLVED = {"type": "judge", "rubric": "resolved"}
STATE = {"type": "state", "expr": "true", "description": "visit booked"}


def row(sid: str, *causes: str, status: TrialStatus = "ok") -> TrialRow:
    """A trial row as hecklr-report writes it: harness errors have no verdict."""
    excluded = status == "harness_error"
    return TrialRow(
        trial_id=TrialId(new_id()),
        scenario_id=sid,
        persona_id="p",
        channel=Channel.CHAT,
        index=0,
        status=status,
        passed=None if excluded else status == "ok" and not causes,
        excluded="harness_error" if excluded else None,
        causes=[FailureCause(cause=cause) for cause in causes],
        duration_ms=1,
        cost_usd=Decimal(0),
    )


def compare(
    baseline: list[TrialRow], candidate: list[TrialRow], scenarios: dict[str, Scenario]
) -> list[fm.GraderResult]:
    runs = (RunId(new_id()), RunId(new_id()))
    return fm.compare_grader_rows(baseline, candidate, scenarios, STATS, runs=runs, seed=7)


def estimate(value: float) -> Estimate:
    return Estimate(
        value=value, ci_low=value - 0.1, ci_high=value + 0.1, n=6, method=IntervalMethod.WILSON
    )


def grader(name: str, verdict: Verdict) -> fm.GraderResult:
    return fm.GraderResult(
        grader=name,
        n_scenarios=6,
        baseline=estimate(1.0),
        candidate=estimate(0.0),
        delta=estimate(-1.0),
        p_value=0.03,
        verdict=verdict,
    )


# --- grader attribution ----------------------------------------------------------------------


def test_grader_families_follow_the_spec_table() -> None:
    tool_called = {"type": "tool_called", "tool": "book_appointment"}
    args_match = {"type": "args_match", "tool": "book_appointment", "args": {"slot": "x"}}
    no_refund = {"type": "no_unauthorized_refund"}
    families = fm.families_by_label(
        {"s": scenario("s", RESOLVED, STATE, tool_called, args_match, no_refund)}
    )
    assert families == {
        "s": {
            "judge:resolved": "judge:resolved",
            "state: visit booked": "state",
            "tool_called book_appointment": "tool",
            "args_match book_appointment": "tool",
            "no_unauthorized_refund": "no_unauthorized_refund",
        }
    }


def test_sc8_f1_booking_the_wrong_slot_first_regresses_the_tool_grader() -> None:
    """F1 on luna: the corrected booking satisfies tool_called; args_match sees the first one."""
    tool_called = {"type": "tool_called", "tool": "book_appointment", "args": {"slot": "x"}}
    args_match = {"type": "args_match", "tool": "book_appointment", "args": {"slot": "x"}}
    ids = [f"s{i}" for i in range(6)]
    scenarios = {sid: scenario(sid, tool_called, args_match) for sid in ids}
    baseline = [row(sid) for sid in ids]
    candidate = [row(sid, "args_match book_appointment") for sid in ids]
    (tool,) = compare(baseline, candidate, scenarios)
    assert (tool.grader, tool.verdict) == ("tool", Verdict.REGRESSION)


def test_grader_outcomes_count_only_scenarios_carrying_the_grader() -> None:
    families = {"a": {"judge:resolved": "judge:resolved"}, "b": {"state: x": "state"}}
    rows = [
        row("a", "judge:resolved"),
        row("a"),
        row("a", status="timeout"),
        row("a", status="harness_error"),
        row("b", "state: x"),
    ]
    outcomes = fm.grader_outcomes(rows, "judge:resolved", families)
    assert [(o.scenario_id, o.passed) for o in outcomes] == [
        ("a", False),
        ("a", True),
        ("a", False),
    ]


def test_sc8_a_fault_regresses_on_its_grader_and_not_on_the_others() -> None:
    ids = [f"s{i}" for i in range(6)]
    scenarios = {sid: scenario(sid, RESOLVED, STATE) for sid in ids}
    baseline = [row(sid) for sid in ids for _ in range(2)]
    candidate = [row(sid, "judge:resolved") for sid in ids for _ in range(2)]
    results = {r.grader: r for r in compare(baseline, candidate, scenarios)}
    assert results["judge:resolved"].verdict is Verdict.REGRESSION
    assert results["judge:resolved"].delta.value == -1.0
    assert results["state"].verdict is Verdict.NO_CHANGE
    assert results["state"].n_scenarios == 6


def test_a_grader_with_no_shared_scenario_is_not_compared() -> None:
    scenarios = {"a": scenario("a", RESOLVED), "b": scenario("b", STATE)}
    assert compare([row("a")], [row("b")], scenarios) == []


@pytest.mark.parametrize(
    ("expected", "gate_exit", "flagged", "caught"),
    [
        ((), 0, (), True),
        ((), 2, (), True),  # inconclusive is not a false alarm
        ((), 1, (), False),
        ((), 3, (), False),
        (("state",), 1, ("state",), True),
        (("state",), 1, (), False),  # the suite regressed, but not on the spec's grader
        (("state",), 2, ("state",), False),  # the gate did not fail
        (("tool", "judge:resolved"), 1, ("tool",), False),
    ],
)
def test_sc8_pass_rule(
    expected: tuple[str, ...], gate_exit: int, flagged: tuple[str, ...], caught: bool
) -> None:
    graders = [grader(name, Verdict.REGRESSION) for name in flagged]
    graders.append(grader("other", Verdict.NO_CHANGE))
    assert fm.is_caught(expected, gate_exit, graders) is caught


def _row(
    variant: str,
    expected: list[str],
    gate_exit: int,
    verdict: Verdict | None,
    graders: list[fm.GraderResult],
    caught: bool,
) -> fm.Row:
    return fm.Row(
        variant=variant,
        run_id=str(new_id()),
        expected=expected,
        gate_exit=gate_exit,
        verdict=verdict,
        delta=estimate(-0.2) if verdict else None,
        p_value=0.01 if verdict else None,
        graders=graders,
        caught=caught,
        hecklr_spent_usd=Decimal(0),
        agent_spent_usd=Decimal(0),
        report_dir="r",
    )


def _matrix(rows: list[fm.Row]) -> fm.Matrix:
    return fm.Matrix(
        created_at=datetime.now(UTC),
        baseline_hecklr_spent_usd=Decimal(0),
        baseline_agent_spent_usd=Decimal(0),
        tier="a",
        suite="matrix",
        brain="rules",
        simulator_model=None,
        judge_model="openai/gpt-6-luna",
        agent_model=None,
        seed=1,
        baseline_run_id="B",
        rows=rows,
        budget_usd=Decimal("0.5"),
        spent_usd=Decimal("0.1"),
        passed=all(row.caught for row in rows),
    )


def test_the_table_names_every_outcome() -> None:
    rows = [
        _row("clean-aa", [], 1, Verdict.REGRESSION, [], False),
        _row("F2", ["state"], 1, Verdict.REGRESSION, [grader("state", Verdict.REGRESSION)], True),
        _row("F3", ["judge:no_hallucination"], 3, None, [], False),
    ]
    text = fm.render_markdown(_matrix(rows))
    assert "tier a: FAIL" in text
    assert "| clean-aa | (nothing: A/A) | regression |" in text
    assert "FALSE ALARM" in text
    assert "**state** -100.0 pts [-110.0, -90.0] p=0.030 regression" in text
    assert "| F3 | judge:no_hallucination | none | n/a | n/a | 3 | none | MISSED |" in text
    assert rows[1].flagged == ["state"]


def test_sc5_an_aa_grader_regression_is_shown_but_not_gated() -> None:
    graders = [grader("tool", Verdict.REGRESSION), grader("state", Verdict.NO_CHANGE)]
    aa = _row("clean-aa", [], 2, Verdict.INCONCLUSIVE, graders, True)
    text = fm.render_markdown(_matrix([aa]))
    assert "tier a: PASS" in text
    assert "no false alarm (graders regressed, not gated: tool) |" in text


# --- configuration, budget, parsing ----------------------------------------------------------


def test_the_derived_config_points_at_the_agent_and_a_private_store(tmp_path: Path) -> None:
    source = {
        "agent": {"adapters": {"chat": {"url": "http://127.0.0.1:8000/chat/stream"}}},
        "budget": {"usd": 5, "concurrency": 4},
    }
    derived = fm.derive_config(source, port=9123, store=tmp_path, concurrency=12)
    assert derived["agent"] == {"adapters": {"chat": {"url": "http://127.0.0.1:9123/chat/stream"}}}
    assert derived["output_dir"] == str(tmp_path)
    assert derived["budget"] == {"usd": 5, "concurrency": 12}
    assert source["budget"] == {"usd": 5, "concurrency": 4}
    with pytest.raises(fm.MatrixError, match="cannot point"):
        fm.derive_config(
            {"agent": {"adapters": {"chat": {"url": "http://localhost:8000/chat"}}}},
            port=9123,
            store=tmp_path,
            concurrency=None,
        )
    assert fm.derive_config(source, port=1, store=tmp_path, concurrency=None)["budget"] == {
        "usd": 5,
        "concurrency": 4,
    }


def test_the_project_copy_holds_the_suite_files(tmp_path: Path) -> None:
    config = fm.prepare_project(tmp_path, 9123, None)
    assert config == tmp_path / "project" / "hecklr.yaml"
    copied = {p.name for p in (tmp_path / "project" / "scripted" / "judged").iterdir()}
    assert len(copied) == 18
    assert not (tmp_path / "project" / "agent").exists()


def test_the_budget_keeps_two_pools_under_one_cap() -> None:
    budget = fm.Budget.split(Decimal("3.5"), Decimal("0.8"))
    assert (budget.hecklr_usd, budget.agent_usd, budget.total) == (
        Decimal("2.80"),
        Decimal("0.70"),
        Decimal("3.5"),
    )
    budget.hecklr_spent += Decimal("0.123456")
    budget.agent_spent += Decimal("0.9")
    assert budget.hecklr_left() == Decimal("2.6765")
    assert budget.agent_left() == 0
    assert budget.spent == Decimal("1.023456")


def health(variant: str = "F1", /, **failed: int) -> fm.Health:
    return fm.Health(
        variant=variant,
        spent_usd=Decimal("0.1"),
        requests_by_world_source={"header": 30, "local": 0},
        failed_turns=failed,
    )


def test_a_clean_agent_run_is_accepted() -> None:
    fm.check_health(health("clean"), "clean-aa")
    fm.check_health(health(LLMOutputError=1), "F1")  # the agent's own bad output counts
    # M1 Tier B F1: a reply that hit the output limit came back as a provider 400.
    fm.check_health(health(**{"LLMProviderError[400]": 1}), "F1")


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        (health("F2"), "ran 'F2', not 'F1'"),
        (fm.Health("F1", Decimal(0), {"header": 3, "local": 2}, {}), "outside the trial"),
        (health(LLMProviderError=21), "exhausted quota"),
        (health(**{"LLMProviderError[429]": 21}), "exhausted quota"),
        (health(**{"LLMProviderError[503]": 1}), "outage"),
        (health(AgentBudgetError=1), "AgentBudgetError"),
    ],
)
def test_a_run_that_did_not_measure_the_variant_is_refused(bad: fm.Health, match: str) -> None:
    """M1 Tier B: an exhausted OpenAI quota turned F1's turns into agent errors, which the
    per-grader comparison would have counted as a caught fault."""
    with pytest.raises(fm.MatrixError, match=match):
        fm.check_health(bad, "F1")


def test_the_run_id_comes_from_hecklr_runs_output() -> None:
    out = "Spent: $0.0000 of budget $0.10\nRun 01M5GDZ04C453482PASREZA5P9 (completed)\nResults: x\n"
    assert fm.recorded_run(out) == "01M5GDZ04C453482PASREZA5P9"
    styled = (
        "\x1b[1mRun\x1b[0m \x1b[36m01M5GDZ04C453482PASREZA5P9\x1b[0m (\x1b[32mcompleted\x1b[0m)\n"
    )
    assert fm.recorded_run(styled) == "01M5GDZ04C453482PASREZA5P9"
    # CI sets FORCE_COLOR, so Rich styles the id: the run line then carries ANSI escapes.
    styled = "Run \x1b[1m01M5GDZ04C453482PASREZA5P9\x1b[0m (completed)\n"
    assert fm.recorded_run(styled) == "01M5GDZ04C453482PASREZA5P9"
    with pytest.raises(fm.MatrixError, match="no run id"):
        fm.recorded_run("error: the run failed")


def test_a_tier_can_run_on_another_suite() -> None:
    """M1: F4 was re-measured on the data-protection suite after its scenarios were fixed."""
    assert fm.parse_args(["--tier", "b", "--suite", "data-protection"]).suite == "data-protection"
    assert fm.parse_args(["--tier", "b"]).suite is None


def test_unknown_variants_are_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert fm.main(["--tier", "free", "--variants", "F1,F13"]) == 2
    assert "F13" in capsys.readouterr().err


def test_the_free_tier_expects_only_deterministic_graders() -> None:
    graders = {g for names in fm.TIERS["free"].expected.values() for g in names}
    assert not any(g.startswith("judge:") for g in graders)
    assert fm.TIERS["a"].expected == fm.SPEC_GRADERS
    tagged = {s.id for s in load_scenarios("scripted/*.yaml") if "catches-f3" in s.tags}
    assert all(
        any(c.type == "not_contains" for c in s.success)
        for s in load_scenarios("scripted/*.yaml")
        if s.id in tagged
    )


def recorded(variant: str, seed: int, digest: str, model: str | None = AGENT) -> fm.RecordedRun:
    return fm.RecordedRun(
        tier="b",
        suite="matrix-live",
        variant=variant,
        seed=seed,
        trials=None,
        suite_digest=digest,
        agent_model=model,
        run_id=f"run-{variant}-{seed}",
        hecklr_spent_usd=Decimal("0.3"),
        agent_spent_usd=Decimal("0.1"),
        failed_turns={},
    )


def setup(tmp_path: Path) -> tuple[Path, Path]:
    """A project copy with one scenario, and the source ``hecklr.yaml`` it was derived from."""
    project = tmp_path / "project"
    (project / "scenarios").mkdir(parents=True)
    (project / "scenarios" / "a.yaml").write_text("id: a\n")
    (project / "hecklr.yaml").write_text("port: 1\n")
    source = tmp_path / "hecklr.yaml"
    source.write_text("suites: {s: {trials: 3}}\nmodels: {simulator: x/one}\n")
    return project, source


def test_resume_reuses_only_a_run_of_the_same_variant_seed_setup_and_agent_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACME_MODEL", AGENT)
    project, source = setup(tmp_path)
    monkeypatch.setattr(fm, "PROJECT", tmp_path)  # the source hecklr.yaml is tmp_path's
    digest = fm.suite_digest(project, source)
    lines = [
        recorded("clean", 7, digest),
        recorded("clean-aa", 8, "a-digest-of-another-setup"),
        recorded("F1", 99, digest),
        recorded("F2", 9, digest, model="openai/another-model"),
    ]
    (tmp_path / "runs.jsonl").write_text("".join(r.model_dump_json() + "\n" for r in lines))
    budget = fm.Budget.split(Decimal(4), Decimal("0.7"))
    runner = fm.Runner(
        "b",
        fm.TIERS["b"],
        tmp_path,
        project / "hecklr.yaml",
        0,
        7,
        budget,
        None,
        {},
        sys.stdout,
        resume=True,
    )

    record = runner.run_variant("clean", 0)

    assert (record.run_id, budget.spent) == ("run-clean-7", Decimal("0.4"))
    assert runner._recorded("clean-aa", 8, digest) is None  # recorded under another setup
    assert runner._recorded("F1", 9, digest) is None  # another seed
    assert runner._recorded("F2", 9, digest) is None  # another agent model


def test_an_unreadable_runs_file_is_a_matrix_error(tmp_path: Path) -> None:
    (tmp_path / "runs.jsonl").write_text('{"tier": "b", "variant": "clean"}\n')  # an old record
    with pytest.raises(fm.MatrixError, match="rerun without --resume"):
        fm.load_recorded(tmp_path / "runs.jsonl")


def test_the_setup_digest_covers_suite_files_and_the_source_config_not_the_port(
    tmp_path: Path,
) -> None:
    project, source = setup(tmp_path)
    digest = fm.suite_digest(project, source)

    (project / "hecklr.yaml").write_text("port: 2\n")  # another invocation's port and store
    assert fm.suite_digest(project, source) == digest
    (project / "scenarios" / "a.yaml").write_text("id: b\n")
    changed_scenario = fm.suite_digest(project, source)
    assert changed_scenario != digest
    source.write_text("suites: {s: {trials: 5}}\nmodels: {simulator: x/one}\n")
    assert fm.suite_digest(project, source) != changed_scenario
    changed_trials = fm.suite_digest(project, source)
    source.write_text("suites: {s: {trials: 5}}\nmodels: {simulator: x/two}\n")
    assert fm.suite_digest(project, source) != changed_trials


# --- real processes --------------------------------------------------------------------------


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_an_agent_that_cannot_start_is_a_matrix_error(tmp_path: Path) -> None:
    with (
        (tmp_path / "agent.log").open("w") as log,
        pytest.raises(fm.MatrixError, match="exited with code 2"),
        fm.agent_server(
            brain="rules", fault="F99", port=fm.free_port(), seed=1, cap_usd=None, log=log
        ),
    ):
        pass
    assert "no fault variant 'F99'" in (tmp_path / "agent.log").read_text()


@pytest.mark.slow
@pytest.mark.e2e
@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
def test_sc8_free_tier_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real thing on a slice of the matrix: agent over HTTP, hecklr run, gate, table."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    code = fm.main(["--tier", "free", "--out", str(tmp_path), "--variants", "clean-aa,F5"])
    captured = capsys.readouterr()
    printed = captured.out
    logs = "\n".join(
        f"== {log.name}\n{log.read_text()[-3000:]}" for log in sorted((tmp_path / "logs").glob("*"))
    )
    assert code == 0, f"{captured.err}\n{printed}\n{logs}"
    matrix = json.loads((tmp_path / "matrix.json").read_text())
    assert matrix["passed"] is True
    assert matrix["spent_usd"] == "0"
    aa, f5 = matrix["rows"]
    assert (aa["variant"], aa["verdict"], aa["gate_exit"]) == ("clean-aa", "no_change", 0)
    assert (f5["variant"], f5["verdict"], f5["gate_exit"]) == ("F5", "regression", 1)
    flagged = {g["grader"] for g in f5["graders"] if g["verdict"] == "regression"}
    assert "no_unauthorized_refund" in flagged
    assert (tmp_path / "reports" / "F5" / "report.html").is_file()
    assert (tmp_path / "reports" / "clean" / "report.html").is_file()
    assert "tier free: PASS" in printed

    # --resume reuses every recorded run: the same run ids, no new run, the same verdicts.
    assert len((tmp_path / "runs.jsonl").read_text().splitlines()) == 3
    resumed = fm.main(
        ["--tier", "free", "--out", str(tmp_path), "--variants", "clean-aa,F5", "--resume"]
    )
    again = capsys.readouterr().out
    assert resumed == 0, again
    assert again.count("(resumed)") == 3
    assert len((tmp_path / "runs.jsonl").read_text().splitlines()) == 3
    rerun = json.loads((tmp_path / "matrix.json").read_text())
    assert [r["run_id"] for r in rerun["rows"]] == [aa["run_id"], f5["run_id"]]
    assert rerun["baseline_run_id"] == matrix["baseline_run_id"]
