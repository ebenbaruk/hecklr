"""analyze(): stored results to statistics, verdicts, flips and counts."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from pydantic import TypeAdapter

from hecklr_core import RunResults
from hecklr_report import EXIT_HARNESS_ERROR, Analysis, analyze, gate_exit_code
from hecklr_report.analysis import COST_PER_TRIAL
from hecklr_report.outcomes import criterion_label, snr_bucket, trial_verdict
from hecklr_schemas import (
    Channel,
    GateConfig,
    IntervalMethod,
    RunStatus,
    Scenario,
    SuccessCriterion,
    Verdict,
)
from hecklr_stats import JudgeCalibration, wilson_estimate

if TYPE_CHECKING:
    from conftest import Kit

GOOD = 0.9
BAD = 0.1


def build(
    kit: type[Kit],
    rates: dict[str, float],
    *,
    seed: int,
    rng_seed: int,
    trials: int = 5,
    **kw: object,
) -> RunResults:
    builder = kit.RunBuilder(seed=seed)
    builder.suite(rates, trials, rng_seed=rng_seed, **kw)
    return builder.build()


def rates(n: int, bad: int = 0) -> dict[str, float]:
    return {f"s{i:02d}": (BAD if i < bad else GOOD) for i in range(n)}


def test_us04_bad_candidate_is_a_regression(kit: type[Kit], scenarios: dict[str, Scenario]) -> None:
    baseline = build(kit, rates(40), seed=1, rng_seed=1)
    candidate = build(kit, rates(40, bad=12), seed=2, rng_seed=2)

    analysis = analyze(candidate, baseline, kit.stats, scenarios)

    suite = analysis.suite_comparison
    assert suite is not None
    assert analysis.verdict is Verdict.REGRESSION
    assert suite.delta.value < -kit.stats.min_effect
    assert suite.delta.ci_high < 0
    assert suite.p_value < kit.stats.alpha
    assert analysis.comparisons[0] is suite
    pass_to_fail = [g for g in analysis.flips if g.direction == "pass_to_fail"]
    flipped = {s for g in pass_to_fail for s in g.scenario_ids}
    degraded = {f"s{i:02d}" for i in range(12)}
    # 5 trials at 90% can still flip by chance; nearly every flip must be a degraded scenario.
    assert len(flipped & degraded) >= 10
    assert len(flipped - degraded) <= 1
    assert pass_to_fail[0].cause == "judge:resolved"
    assert pass_to_fail[0].rationale == "The agent never confirmed the new slot."


def test_us04_identical_agent_is_not_a_regression(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    baseline = build(kit, rates(40), seed=1, rng_seed=1)
    candidate = build(kit, rates(40), seed=2, rng_seed=2)

    analysis = analyze(candidate, baseline, kit.stats, scenarios)

    assert analysis.verdict in (Verdict.NO_CHANGE, Verdict.INCONCLUSIVE)
    assert all(
        c.verdict is not Verdict.REGRESSION for c in analysis.comparisons if c.metric == "pass_rate"
    )


def test_us01_pass_rate_with_interval_per_scenario_and_suite(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    candidate = build(kit, rates(10, bad=3), seed=1, rng_seed=3)

    analysis = analyze(candidate, None, kit.stats, scenarios)

    suite = analysis.summary("pass_rate")
    assert suite is not None
    assert suite.method is IntervalMethod.CLUSTER_BOOTSTRAP
    assert suite.ci_low <= suite.value <= suite.ci_high
    assert suite.n == 10
    assert analysis.summary("pass^3") is not None
    assert analysis.summary("pass@3") is not None
    assert analysis.summary("response_latency_p95") is not None
    cost = analysis.summary(COST_PER_TRIAL)
    assert cost is not None
    assert cost.value == pytest.approx(0.01)
    for result in analysis.scenarios:
        assert result.pass_rate is not None
        assert result.pass_rate == wilson_estimate(result.passes, result.trials)
    assert analysis.verdict is None
    assert analysis.comparisons == []
    assert analysis.problems == []


def test_harness_errors_are_excluded_but_counted(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    builder = kit.RunBuilder()
    builder.trial("s00", passed=True)
    builder.trial("s00", passed=False, status="harness_error")
    builder.trial("s01", passed=False, status="agent_error")
    builder.trial("s01", passed=False, status="timeout")

    analysis = analyze(builder.build(), None, kit.stats, scenarios)

    counts = analysis.counts
    assert (counts.total, counts.analyzed, counts.harness_error) == (4, 3, 1)
    assert (counts.agent_error, counts.timeout, counts.ok) == (1, 1, 1)
    s00 = next(s for s in analysis.scenarios if s.scenario_id == "s00")
    assert (s00.trials, s00.passes) == (1, 1)
    excluded = [t for t in analysis.trials if t.excluded]
    assert [t.excluded for t in excluded] == ["harness_error"]


def test_agent_errors_fail_even_when_every_grade_passed(kit: type[Kit]) -> None:
    builder = kit.RunBuilder()
    trial = builder.trial("s00", passed=True, status="agent_error")
    grades = builder.build().grades_of(trial)

    assert all(g.passed for g in grades)
    assert trial_verdict(trial, grades) == (False, None)


def test_grade_errors_leave_a_trial_undecided_unless_another_criterion_failed(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    builder = kit.RunBuilder()
    undecided = builder.trial("s00", passed=True, judge_error=True)
    failed = builder.trial("s00", passed=True, judge_error=True, tool_failed=True)
    results = builder.build()

    assert trial_verdict(undecided, results.grades_of(undecided)) == (None, "grade_error")
    assert trial_verdict(failed, results.grades_of(failed)) == (False, None)
    analysis = analyze(results, None, kit.stats, scenarios)
    assert (analysis.counts.undecided, analysis.counts.grade_errors) == (1, 2)
    assert analysis.counts.analyzed == 1


def test_failure_causes_name_the_failing_criterion(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    builder = kit.RunBuilder()
    builder.trial("s00", passed=True, tool_failed=True)
    builder.trial("s01", passed=False, status="timeout")

    analysis = analyze(builder.build(), None, kit.stats, scenarios)

    causes = {t.scenario_id: [c.cause for c in t.causes] for t in analysis.trials}
    assert causes["s00"] == ["tool_called reschedule_appointment", "judge:resolved"]
    assert causes["s01"] == ["timeout", "judge:resolved"]


def test_scenarios_missing_from_the_definitions_are_still_analyzed(kit: type[Kit]) -> None:
    builder = kit.RunBuilder()
    builder.trial("unknown", passed=False)

    analysis = analyze(builder.build(), None, kit.stats, {})

    result = analysis.scenarios[0]
    assert (result.title, result.tags) == ("unknown", [])
    assert result.cause is not None
    assert result.cause.cause == "judge:resolved"


def test_fail_to_pass_flips_report_the_baseline_cause(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    baseline = build(kit, rates(40, bad=12), seed=1, rng_seed=1)
    candidate = build(kit, rates(40), seed=2, rng_seed=2)

    analysis = analyze(candidate, baseline, kit.stats, scenarios)

    assert analysis.verdict is Verdict.IMPROVEMENT
    fixed = [g for g in analysis.flips if g.direction == "fail_to_pass"]
    assert fixed
    assert fixed[0].cause == "judge:resolved"


def test_slices_are_compared_with_bh_q_values(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    baseline = build(kit, rates(40), seed=1, rng_seed=1)
    candidate = build(kit, rates(40, bad=12), seed=2, rng_seed=2)

    analysis = analyze(candidate, baseline, kit.stats, scenarios)

    slices = [c for c in analysis.comparisons if c.metric == "pass_rate" and c.slice]
    assert {"tag": "billing"} in [c.slice for c in slices]
    assert {"channel": "chat"} in [c.slice for c in slices]
    assert all(c.q_value >= c.p_value for c in slices)


def test_latency_regression_is_detected_and_reported_per_channel(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    baseline = build(kit, rates(20), seed=1, rng_seed=1, latencies_ms=(400, 500, 600))
    candidate = build(kit, rates(20), seed=2, rng_seed=1, latencies_ms=(1_100, 1_200, 1_300))

    analysis = analyze(candidate, baseline, kit.stats, scenarios)

    p95 = [c for c in analysis.comparisons if c.metric == "response_latency_p95"]
    assert [c.slice for c in p95] == [{}, {"channel": "chat"}]
    suite = p95[0]
    assert suite.delta.value == pytest.approx(700)
    assert suite.verdict is Verdict.REGRESSION
    assert suite.min_effect == pytest.approx(kit.stats.min_effect * 600)
    assert analysis.verdict is not Verdict.REGRESSION


def test_identical_latencies_are_not_a_latency_regression(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    baseline = build(kit, rates(20), seed=1, rng_seed=1)
    candidate = build(kit, rates(20), seed=2, rng_seed=1)

    analysis = analyze(candidate, baseline, kit.stats, scenarios)

    latency = [c for c in analysis.comparisons if c.metric.startswith("response_latency")]
    assert latency
    assert all(c.verdict is not Verdict.REGRESSION for c in latency)
    assert all(c.delta.value == 0 for c in latency)


def test_problems_block_comparison(kit: type[Kit], scenarios: dict[str, Scenario]) -> None:
    baseline = build(kit, {"s00": GOOD}, seed=1, rng_seed=1)
    candidate = build(kit, {"s01": GOOD}, seed=2, rng_seed=1)

    analysis = analyze(candidate, baseline, kit.stats, scenarios)

    assert analysis.problems == ["the candidate and baseline runs share no analyzable scenario"]
    assert analysis.comparisons == []
    assert analysis.verdict is None


def test_broken_runs_and_empty_runs_are_problems(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    candidate = kit.RunBuilder(status=RunStatus.FAILED)
    candidate.trial("s00", status="harness_error")
    baseline = kit.RunBuilder(status=RunStatus.CANCELLED)
    baseline.trial("s00", status="harness_error")

    analysis = analyze(candidate.build(), baseline.build(), kit.stats, scenarios)

    assert len(analysis.problems) == 4
    assert analysis.summaries == []
    assert analysis.scenarios[0].pass_rate is None


def test_issue33_a_run_where_no_trial_got_through_is_a_problem(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    candidate = kit.RunBuilder()
    for scenario_id in ("s00", "s01"):
        candidate.trial(scenario_id, status="agent_error", passed=False)
    candidate.trial("s02", status="timeout", passed=False)
    candidate.trial("s03", status="harness_error")

    analysis = analyze(candidate.build(), None, kit.stats, scenarios)

    assert analysis.problems == [
        "no trial of the candidate run got through: 2 agent errors and 1 timeout, first "
        "'agent returned HTTP 500'; is the agent up and its adapter configured right?"
    ]
    assert gate_exit_code(analysis, GateConfig()) == EXIT_HARNESS_ERROR


def test_issue33_a_baseline_where_no_trial_got_through_is_a_problem(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    baseline = kit.RunBuilder()
    baseline.trial("s00", status="timeout", passed=False)
    candidate = kit.RunBuilder()
    candidate.trial("s00")

    analysis = analyze(candidate.build(), baseline.build(), kit.stats, scenarios)

    assert analysis.problems == [
        "no trial of the baseline run got through: 1 timeout; is the agent up and its adapter "
        "configured right?"
    ]


def test_issue33_one_trial_that_got_through_is_a_measurement(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    candidate = kit.RunBuilder()
    candidate.trial("s00", status="agent_error", passed=False)
    candidate.trial("s01", passed=False)

    analysis = analyze(candidate.build(), None, kit.stats, scenarios)

    assert analysis.problems == []


def test_contradiction_rate_has_a_wilson_interval(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    builder = kit.RunBuilder()
    builder.trial("s00", contradictions=2)
    builder.trial("s00")

    analysis = analyze(builder.build(), None, kit.stats, scenarios)

    assert analysis.counts.contradiction_rate == wilson_estimate(1, 2)


def test_judge_calibrations_flag_untrusted_rubrics(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    builder = kit.RunBuilder()
    builder.trial("s00")
    kappa = wilson_estimate(5, 10)
    calibration = JudgeCalibration(
        grader="judge:resolved",
        grader_version="abc123",
        judge_model="fake/judge",
        n=10,
        human_passes=5,
        human_fails=5,
        kappa=kappa,
        precision=None,
        recall=None,
    )

    analysis = analyze(
        builder.build(), None, kit.stats, scenarios, judge_calibrations=[calibration]
    )

    assert [(j.grader, j.trusted, j.kappa) for j in analysis.judges] == [
        ("judge:resolved", False, kappa)
    ]


def test_analysis_is_reproducible_and_round_trips_through_json(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    baseline = build(kit, rates(12), seed=1, rng_seed=1)
    candidate = build(kit, rates(12, bad=3), seed=2, rng_seed=2)

    first = analyze(candidate, baseline, kit.stats, scenarios)
    second = analyze(candidate, baseline, kit.stats, scenarios)

    assert [c.delta for c in first.comparisons] == [c.delta for c in second.comparisons]
    assert first.summaries == second.summaries
    assert Analysis.model_validate_json(first.model_dump_json()) == first


def test_run_seed_is_used_when_stats_has_none(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    candidate = build(kit, rates(4), seed=99, rng_seed=1)

    analysis = analyze(candidate, None, kit.stats.model_copy(update={"seed": None}), scenarios)

    assert analysis.seed == 99


@pytest.mark.parametrize(
    ("snr_db", "bucket"),
    [
        (None, "clean"),
        (35.0, "20"),
        (20.0, "20"),
        (19.9, "10"),
        (12.0, "10"),
        (5.0, "5"),
        (4.0, "0"),
        (-3.0, "0"),
    ],
)
def test_us07_snr_buckets(snr_db: float | None, bucket: str) -> None:
    assert snr_bucket(snr_db) == bucket


def test_us06_us07_voice_slices_by_noise_and_accent(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    personas = {
        "clean": kit.persona("clean", accent="en-GB"),
        "noisy-10db": kit.persona("noisy-10db", snr_db=10, accent="fr-FR"),
        "noisy-0db": kit.persona("noisy-0db", snr_db=0, accent="fr-FR"),
    }
    builder = kit.RunBuilder()
    shape = kit.VoiceShape(barge_in_reaction_ms=250, dead_air_ms=3_000)
    for persona, rate in (("clean", 0.9), ("noisy-10db", 0.6), ("noisy-0db", 0.2)):
        builder.suite(
            {f"s{i:02d}": rate for i in range(8)},
            3,
            rng_seed=4,
            channel=Channel.VOICE,
            persona_id=persona,
            voice=shape,
            latencies_ms=(700, 900),
        )
    builder.trial("s00", channel=Channel.CHAT)

    analysis = analyze(builder.build(), None, kit.stats, scenarios, personas=personas)

    voice = analysis.voice
    assert voice is not None
    assert [p.label for p in voice.degradation] == ["clean", "10", "0"]
    assert [p.trials for p in voice.degradation] == [24, 24, 24]
    values = [p.pass_rate.value for p in voice.degradation]
    assert values == sorted(values, reverse=True)
    assert [p.label for p in voice.by_accent] == ["en-GB", "fr-FR"]
    metrics = {(m.metric, tuple(m.slice.items())) for m in voice.metrics}
    for slice_ in ((), (("snr_db", "10"),), (("accent", "fr-FR"),)):
        for metric in (
            "response_latency_p50",
            "response_latency_p95",
            "response_latency_p99",
            "barge_in_reaction_p95",
            "false_interruptions_per_100_turns",
            "dead_air_per_call",
        ):
            assert (metric, slice_) in metrics
    barge = next(m for m in voice.metrics if m.metric == "barge_in_reaction_p95" and not m.slice)
    assert barge.estimate.value == 250
    dead_air = next(m for m in voice.metrics if m.metric == "dead_air_per_call" and not m.slice)
    assert dead_air.estimate.value == 1.0


def test_unanswered_barge_in_lasts_until_the_agent_stops(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    builder = kit.RunBuilder()
    builder.trial("s00", channel=Channel.VOICE, voice=kit.VoiceShape(barge_in_reaction_ms=None))

    analysis = analyze(builder.build(), None, kit.stats, scenarios)

    assert analysis.voice is not None
    barge = next(m for m in analysis.voice.metrics if m.metric == "barge_in_reaction_p50")
    # The agent span lasts 2,000 ms and the barge-in comes 1,000 ms into it.
    assert barge.estimate.value == 1_000
    assert analysis.voice.degradation == []


def test_no_voice_section_for_chat_only_runs(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    builder = kit.RunBuilder()
    builder.trial("s00")

    assert analyze(builder.build(), None, kit.stats, scenarios).voice is None


@pytest.mark.parametrize(
    ("criterion", "label"),
    [
        ({"type": "tool_not_called", "tool": "issue_refund"}, "tool_not_called issue_refund"),
        ({"type": "args_match", "tool": "book", "patterns": {"d": ".*"}}, "args_match book"),
        ({"type": "call_order", "tools": ["verify", "refund"]}, "call_order verify > refund"),
        ({"type": "state", "expr": "world.x == 1"}, "state: world.x == 1"),
        ({"type": "state", "expr": "world.x == 1", "description": "moved"}, "state: moved"),
        ({"type": "voice_metric", "metric": "dead_air", "below": 1}, "voice:dead_air"),
        ({"type": "custom", "name": "mine"}, "custom:mine"),
        ({"type": "contains", "text": "hi"}, "contains"),
    ],
)
def test_criterion_labels_name_what_failed(criterion: dict[str, object], label: str) -> None:
    parsed: SuccessCriterion = TypeAdapter(SuccessCriterion).validate_python(criterion)

    assert criterion_label(parsed) == label


# --- issue #99: scenarios whose every trial was excluded ----------------------------------


def _broken_by_exclusion(
    kit: type[Kit], *, exclude: str, base_rate: float = 0.9, deterministic: bool = False
) -> tuple[RunResults, RunResults]:
    """40 scenarios x 10 trials; the candidate broke 12 and every trial of those is excluded."""
    import random

    rng = random.Random(0)
    base, cand = kit.RunBuilder(seed=1), kit.RunBuilder(seed=2)
    for i in range(40):
        for _ in range(10):
            base.trial(f"s{i:02d}", passed=deterministic or rng.random() < base_rate)
            if i < 12:
                if exclude == "judge":
                    cand.trial(f"s{i:02d}", passed=rng.random() < 0.1, judge_error=True)
                else:
                    cand.trial(f"s{i:02d}", status="harness_error")
            else:
                cand.trial(f"s{i:02d}", passed=deterministic or rng.random() < base_rate)
    return base.build(), cand.build()


@pytest.mark.parametrize("exclude", ["judge", "harness"])
@pytest.mark.parametrize("deterministic", [False, True])
def test_issue99_scenarios_lost_to_exclusions_block_the_gate(
    kit: type[Kit], scenarios: dict[str, Scenario], exclude: str, deterministic: bool
) -> None:
    baseline, candidate = _broken_by_exclusion(kit, exclude=exclude, deterministic=deterministic)

    analysis = analyze(candidate, baseline, kit.stats, scenarios)

    assert analysis.problems == [
        "12 of 40 scenarios both runs ran have no analyzable trial in the candidate (every trial "
        "a harness error or undecided), so they cannot be compared: s00, s01, s02, s03, s04 "
        "and 7 more"
    ]
    assert analysis.verdict is None
    assert gate_exit_code(analysis, GateConfig()) == EXIT_HARNESS_ERROR
    assert gate_exit_code(analysis, GateConfig(on_inconclusive="pass")) == EXIT_HARNESS_ERROR


def test_issue99_a_baseline_scenario_lost_to_exclusions_is_named_too(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    base, cand = kit.RunBuilder(seed=1), kit.RunBuilder(seed=2)
    for sid in ("s00", "s01"):
        cand.trial(sid)
    base.trial("s00")
    base.trial("s01", judge_error=True)

    analysis = analyze(cand.build(), base.build(), kit.stats, scenarios)

    assert analysis.problems == [
        "1 of 2 scenarios both runs ran have no analyzable trial in the baseline (every trial "
        "a harness error or undecided), so they cannot be compared: s01"
    ]


def test_issue99_partly_excluded_and_new_scenarios_still_compare(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    base, cand = kit.RunBuilder(seed=1), kit.RunBuilder(seed=2)
    for sid in ("s00", "s01"):
        base.trial(sid)
        cand.trial(sid)
    cand.trial("s01", status="harness_error")  # one of its two trials still counts
    cand.trial("s02", judge_error=True)  # new in the candidate: never paired anyway

    analysis = analyze(cand.build(), base.build(), kit.stats, scenarios)

    assert analysis.problems == []
    assert analysis.verdict is not None
