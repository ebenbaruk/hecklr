"""Outcomes and the end-of-run summary, on hand-built stored results."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from rich.console import Console

from hecklr.outcomes import trial_outcomes
from hecklr.summary import percent, render_summary, summarize
from hecklr_core import RunResults
from hecklr_schemas import (
    AgentRef,
    Budget,
    Channel,
    Estimate,
    Event,
    EventId,
    Grade,
    GradeId,
    IntervalMethod,
    Run,
    RunCounts,
    RunId,
    RunStatus,
    Trial,
    TrialId,
    TrialStatus,
    UtterancePayload,
    WorldSnapshot,
    new_id,
)

RUN_ID = RunId(new_id())
NOW = datetime(2026, 10, 21, tzinfo=UTC)
EMPTY = WorldSnapshot(world_id="w", taken_at_ms=0, collections={})


def _trial(scenario: str, status: TrialStatus = "ok", contradictions: int = 0) -> Trial:
    return Trial(
        id=TrialId(new_id()),
        run_id=RUN_ID,
        scenario_id=scenario,
        persona_id="p",
        channel=Channel.CHAT,
        index=0,
        seed=1,
        status=status,
        started_at=NOW,
        duration_ms=10,
        cost_usd=Decimal(0),
        world_before=EMPTY,
        world_after=EMPTY,
        contradictions=contradictions,
    )


def _grade(trial: Trial, *, passed: bool, status: str = "ok", index: int | None = 0) -> Grade:
    return Grade.model_validate(
        {
            "id": GradeId(new_id()),
            "trial_id": trial.id,
            "grader": "g",
            "grader_version": "1",
            "criterion_index": index,
            "status": status,
            "value": passed,
            "passed": passed,
        }
    )


def _utterance(trial: Trial, actor: str, seq: int, latency_ms: int | None = None) -> Event:
    return Event.model_validate(
        {
            "id": EventId(new_id()),
            "trial_id": trial.id,
            "seq": seq,
            "t_ms": seq * 10,
            "actor": actor,
            "payload": UtterancePayload(text="hi", turn=seq, latency_ms=latency_ms),
        }
    )


def _results(trials: list[Trial], events: list[Event], grades: list[Grade]) -> RunResults:
    run = Run(
        id=RUN_ID,
        created_at=NOW,
        agent=AgentRef(name="a"),
        config_hash="h",
        seed=0,
        suite="ci",
        matrix=[t.spec for t in trials],
        budget=Budget(usd=Decimal(5)),
        status=RunStatus.COMPLETED,
        counts=RunCounts(
            planned=len(trials),
            ok=sum(t.status == "ok" for t in trials),
            harness_error=sum(t.status == "harness_error" for t in trials),
        ),
    )
    return RunResults(run=run, trials=trials, events=events, grades=grades)


def _sample() -> RunResults:
    good, bad, broken, undecided = (
        _trial("a", contradictions=1),
        _trial("a"),
        _trial("b", "harness_error"),
        _trial("b"),
    )
    events = [
        _utterance(good, "user", 0),
        _utterance(good, "agent", 1, latency_ms=120),
        _utterance(bad, "user", 0),
        _utterance(bad, "agent", 1, latency_ms=300),
        _utterance(broken, "user", 0),
    ]
    grades = [
        _grade(good, passed=True),
        _grade(good, passed=False, status="skipped", index=None),
        _grade(bad, passed=False),
        _grade(undecided, passed=False, status="error"),
    ]
    return _results([good, bad, broken, undecided], events, grades)


def test_outcomes_exclude_harness_and_grading_errors_and_keep_latencies() -> None:
    counted = trial_outcomes(_sample())

    assert (counted.harness_errors, counted.grading_errors) == (1, 1)
    assert [(o.scenario_id, o.passed, o.latencies_ms) for o in counted.outcomes] == [
        ("a", True, (120,)),
        ("a", False, (300,)),
    ]


def test_us01_summary_has_suite_and_scenario_rates_with_intervals() -> None:
    summary = summarize(_sample(), results_dir=Path("runs/x"), resamples=200, seed=3)

    assert summary.suite_pass_rate is not None
    assert summary.suite_pass_rate.method is IntervalMethod.CLUSTER_BOOTSTRAP
    [row] = summary.scenarios
    assert (row.scenario_id, row.passes, row.trials) == ("a", 1, 2)
    assert row.pass_rate.method is IntervalMethod.WILSON
    assert row.pass_rate.ci_low < 0.5 < row.pass_rate.ci_high
    assert set(summary.latencies) == {"response_latency_p50", "response_latency_p95"}
    # Two customer turns in trials that ran; the harness-error trial's turn is not counted.
    assert (summary.contradictions, summary.customer_turns) == (1, 2)
    assert summary.contradiction_rate is not None
    assert summary.contradiction_rate.method is IntervalMethod.WILSON


def test_rendered_summary_shows_every_number_with_its_interval() -> None:
    console = Console(record=True, width=120)
    summary = summarize(_sample(), results_dir=Path("runs/x"), resamples=200, seed=3)

    render_summary(summary, console)

    text = console.export_text()
    assert "Suite pass rate 50% [" in text
    assert "1/2" in text
    assert "response latency p95" in text
    assert "1 harness errors (excluded), 1 grading errors (excluded)" in text
    assert "Simulator contradictions: 1/2" in text
    assert f"Run {RUN_ID} (completed)" in text
    assert "Results: runs/x" in text
    assert "of budget $5.00" in text


def test_summary_of_a_run_where_nothing_could_be_scored() -> None:
    broken = _trial("a", "harness_error")
    results = _results([broken], [], [])
    results = RunResults(
        run=results.run.model_copy(update={"budget": Budget(usd=None)}),
        trials=results.trials,
        events=[],
        grades=[],
    )
    console = Console(record=True, width=120)

    summary = summarize(results, results_dir=Path("r"), resamples=200, seed=0)
    render_summary(summary, console)

    assert summary.suite_pass_rate is None
    assert summary.contradiction_rate is None
    text = console.export_text()
    assert "No trial could be scored" in text
    assert "unlimited" in text


def test_percent_formats_an_estimate() -> None:
    estimate = Estimate(value=0.5, ci_low=0.25, ci_high=0.75, n=4, method=IntervalMethod.WILSON)

    assert percent(estimate) == "50% [25%, 75%]"
