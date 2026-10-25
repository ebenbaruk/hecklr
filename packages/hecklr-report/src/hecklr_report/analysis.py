"""``analyze``: one stored run (and optionally its baseline) to a renderable ``Analysis``.

Statistics come from ``hecklr_stats`` (pass rates, pass^k, pass@k, latency quantiles, paired
comparisons and verdicts); this module converts stored results into its inputs and adds what only
a report needs: flipped scenarios grouped by cause, exclusion counts, voice slices, judge trust
and latency comparisons.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from hecklr_core import RunResults
from hecklr_report.latency import compare_latencies
from hecklr_report.model import (
    Analysis,
    FailureCause,
    FlipDirection,
    FlipGroup,
    JudgeTrust,
    RunInfo,
    ScenarioResult,
    TrialRow,
    VoiceSection,
)
from hecklr_report.outcomes import ACCENT, SNR_DB, ConvertedRun, convert
from hecklr_report.voice import SNR_ORDER, degradation, mean_estimate, voice_metrics
from hecklr_schemas import (
    Channel,
    Comparison,
    Estimate,
    Event,
    MetricSummary,
    Persona,
    RunId,
    RunStatus,
    Scenario,
    StatsConfig,
)
from hecklr_stats import (
    JudgeCalibration,
    TrialOutcome,
    compare_runs,
    compare_sequentially,
    flaky_scenarios,
    summarize_run,
    wilson_estimate,
)
from hecklr_stats.sequential import MIN_LOOK_GAP

COST_PER_TRIAL = "cost_usd_per_trial"
"""Mean simulator and judge spend per conversation, with a bootstrap interval."""

_ERROR_CHARS = 200
"""How much of a trial's error a problem quotes."""

_LISTED_SCENARIOS = 5
"""How many scenario ids a problem names before it says how many more there are."""

_BROKEN_RUN = (RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.PENDING, RunStatus.RUNNING)


def analyze(
    candidate: RunResults,
    baseline: RunResults | None,
    stats: StatsConfig,
    scenarios: Mapping[str, Scenario],
    *,
    personas: Mapping[str, Persona] | None = None,
    judge_calibrations: Sequence[JudgeCalibration] = (),
    identical_runs: bool = False,
) -> Analysis:
    """Analyze ``candidate``, compared with ``baseline`` when one is given.

    Args:
        candidate: The run under test.
        baseline: The run to compare against, or ``None`` for a single-run report.
        stats: Significance level, minimum effect, resamples and seed (the candidate run's seed
            when ``stats.seed`` is ``None``).
        scenarios: Scenario definitions by id, for titles, tags and failure causes. Trials of a
            scenario missing here are still analyzed, without tags.
        personas: Persona definitions by id; voice trials are sliced by SNR bucket and accent only
            when their persona is given.
        judge_calibrations: Agreement of judge rubrics with human labels, when available; rubrics
            below κ 0.70 are flagged untrusted.
        identical_runs: The caller vouches that both runs test the same agent and code, so
            scenarios between 20% and 80% pooled pass rate are flagged flaky; an A/A self-test
            declares it. It is never inferred: a shared commit says nothing about uncommitted
            edits, and pooling a real regression would label it flaky (ADR 0024-hstats).
    """
    known_personas = personas or {}
    seed = stats.seed if stats.seed is not None else candidate.run.seed
    cand = convert(candidate, scenarios, known_personas)
    base = convert(baseline, scenarios, known_personas) if baseline is not None else None
    problems = _problems(candidate, cand, baseline, base)

    summaries = _summaries(candidate, cand, stats, seed)
    baseline_summaries: list[MetricSummary] = []
    comparisons = []
    if baseline is not None and base is not None:
        base_seed = stats.seed if stats.seed is not None else baseline.run.seed
        baseline_summaries = _summaries(baseline, base, stats, base_seed)
        if not problems:
            comparisons = _compare_pass_rates(
                candidate, cand, baseline, base, stats, seed
            ) + compare_latencies(
                base.outcomes,
                cand.outcomes,
                baseline_run=baseline.run.id,
                candidate_run=candidate.run.id,
                config=stats,
                seed=seed,
            )
    identical = baseline is not None and identical_runs
    flaky = (
        {f.scenario_id: f.pass_rate for f in flaky_scenarios([*base.outcomes, *cand.outcomes])}
        if identical and base is not None
        else {}
    )
    scenario_results = _scenario_results(cand.rows, base.rows if base else None, scenarios, flaky)
    return Analysis(
        created_at=datetime.now(UTC),
        candidate=RunInfo.of(candidate.run),
        baseline=RunInfo.of(baseline.run) if baseline is not None else None,
        stats=stats,
        seed=seed,
        counts=cand.counts,
        baseline_counts=base.counts if base is not None else None,
        summaries=summaries,
        baseline_summaries=baseline_summaries,
        comparisons=comparisons,
        verdict=comparisons[0].verdict if comparisons else None,
        scenarios=scenario_results,
        identical_runs=identical,
        flips=_flip_groups(scenario_results),
        voice=_voice(candidate, cand, summaries, baseline_summaries, stats, seed),
        judges=[_trust(c) for c in judge_calibrations],
        trials=cand.rows,
        problems=problems,
    )


def _compare_pass_rates(
    candidate: RunResults,
    cand: ConvertedRun,
    baseline: RunResults,
    base: ConvertedRun,
    stats: StatsConfig,
    seed: int,
) -> list[Comparison]:
    """Pass-rate comparisons: fixed design, or one group-sequential look per adaptive batch."""
    looks = candidate_looks(candidate, cand)
    base_id, cand_id = baseline.run.id, candidate.run.id
    if len(looks) == 1:
        return compare_runs(
            base.outcomes,
            cand.outcomes,
            baseline_run=base_id,
            candidate_run=cand_id,
            config=stats,
            seed=seed,
        )
    return compare_sequentially(
        base.outcomes, looks, baseline_run=base_id, candidate_run=cand_id, config=stats, seed=seed
    )


def candidate_looks(results: RunResults, converted: ConvertedRun) -> list[list[TrialOutcome]]:
    """The candidate's outcomes at each look: the first pass, then everything (issue #103).

    ``hecklr_core`` plans every cell with the same trial indices ``0 .. trials - 1`` in a row,
    and an adaptive pass appends cells numbered from ``trials`` on (``extend_matrix``), so the
    first cell's leading run in the matrix is the first pass's trials per cell. A run without an
    adaptive pass, or whose extra trials are too few to be a look of their own
    (``MIN_LOOK_GAP``), has one look: the fixed design.
    """
    everything = converted.outcomes
    matrix = results.run.matrix
    if not results.run.adaptive or not matrix:
        return [everything]
    first_cell = matrix[0].key[:3]
    per_cell = next((n for n, spec in enumerate(matrix) if spec.key[:3] != first_cell), len(matrix))
    analyzed = [row for row in converted.rows if row.passed is not None]
    first = [
        outcome for row, outcome in zip(analyzed, everything, strict=True) if row.index < per_cell
    ]
    if not first or len(everything) - len(first) < MIN_LOOK_GAP * len(everything):
        return [everything]
    return [first, everything]


def _problems(
    candidate: RunResults,
    cand: ConvertedRun,
    baseline: RunResults | None,
    base: ConvertedRun | None,
) -> list[str]:
    """Reasons the analysis cannot decide a gate (any of them makes the gate exit 3)."""
    problems = []
    if candidate.run.status in _BROKEN_RUN:
        problems.append(f"candidate run {candidate.run.id} is {candidate.run.status.value}")
    if not cand.outcomes:
        problems.append("no trial of the candidate run could be analyzed")
    if (blocked := nothing_got_through(candidate)) is not None:
        problems.append(f"no trial of the candidate run got through: {blocked}")
    if baseline is None or base is None:
        return problems
    if baseline.run.status in _BROKEN_RUN:
        problems.append(f"baseline run {baseline.run.id} is {baseline.run.status.value}")
    if not base.outcomes:
        problems.append("no trial of the baseline run could be analyzed")
    if (blocked := nothing_got_through(baseline)) is not None:
        problems.append(f"no trial of the baseline run got through: {blocked}")
    shared = {o.scenario_id for o in cand.outcomes} & {o.scenario_id for o in base.outcomes}
    if cand.outcomes and base.outcomes and not shared:
        problems.append("the candidate and baseline runs share no analyzable scenario")
    if cand.outcomes and base.outcomes:
        problems.extend(_lost_scenarios(cand, base))
    return problems


def _lost_scenarios(cand: ConvertedRun, base: ConvertedRun) -> list[str]:
    """Scenarios both runs ran that a run has no analyzable trial of (issue #99, ADR 00rtb).

    Pairing keeps only the scenarios both runs analyzed, so a scenario whose every trial was
    excluded (harness error or undecided) in one run would silently leave the comparison: a
    candidate that breaks scenarios in a way that makes their judges error, or the harness fail,
    would be gated on the scenarios it did not break.
    """
    ran = {r.scenario_id for r in cand.rows} & {r.scenario_id for r in base.rows}
    problems = []
    for name, run in (("candidate", cand), ("baseline", base)):
        analyzed = {o.scenario_id for o in run.outcomes}
        lost = sorted(ran - analyzed)
        if not lost:
            continue
        listed = ", ".join(lost[:_LISTED_SCENARIOS])
        more = f" and {len(lost) - _LISTED_SCENARIOS} more" if len(lost) > _LISTED_SCENARIOS else ""
        problems.append(
            f"{len(lost)} of {len(ran)} scenarios both runs ran have no analyzable trial in the "
            f"{name} (every trial a harness error or undecided), so they cannot be compared: "
            f"{listed}{more}"
        )
    return problems


def nothing_got_through(results: RunResults) -> str | None:
    """Why no conversation of the run got through, or ``None`` when one did (ADR 00hra).

    When every conversation ended in an agent error or a timeout, the run measured a broken
    setup (agent down, wrong URL, a reply it cannot parse), not the agent: it must not pass a
    gate or become a baseline (issue #33). Harness errors are already excluded and reported.
    """
    statuses = Counter(trial.status for trial in results.trials)
    errors, timeouts = statuses["agent_error"], statuses["timeout"]
    if statuses["ok"] or not errors + timeouts:
        return None
    tally = [
        f"{count} {name}{'s' if count > 1 else ''}"
        for count, name in ((errors, "agent error"), (timeouts, "timeout"))
        if count
    ]
    first = next((t.error for t in results.trials if t.status != "harness_error" and t.error), None)
    cause = f", first {first[:_ERROR_CHARS]!r}" if first else ""
    return f"{' and '.join(tally)}{cause}; is the agent up and its adapter configured right?"


def _summaries(
    results: RunResults, converted: ConvertedRun, stats: StatsConfig, seed: int
) -> list[MetricSummary]:
    if not converted.outcomes:
        return []
    summaries = summarize_run(
        converted.outcomes,
        run_id=results.run.id,
        seed=seed,
        resamples=stats.bootstrap_resamples,
    )
    costs = [float(r.cost_usd) for r in converted.rows if r.excluded != "harness_error"]
    if costs:
        summaries.append(
            MetricSummary(
                run_id=results.run.id,
                metric=COST_PER_TRIAL,
                estimate=mean_estimate(costs, seed=seed, resamples=stats.bootstrap_resamples),
            )
        )
    return summaries


def _scenario_results(
    rows: Sequence[TrialRow],
    baseline_rows: Sequence[TrialRow] | None,
    scenarios: Mapping[str, Scenario],
    flaky: Mapping[str, Estimate],
) -> list[ScenarioResult]:
    by_scenario = _group(rows)
    base_by_scenario = _group(baseline_rows or [])
    results = []
    for sid in sorted(by_scenario):
        cand_rows = [r for r in by_scenario[sid] if r.passed is not None]
        base_rows = [r for r in base_by_scenario.get(sid, []) if r.passed is not None]
        passes = sum(1 for r in cand_rows if r.passed)
        flip = _flip(cand_rows, base_rows) if baseline_rows is not None else None
        failing_side = base_rows if flip == "fail_to_pass" else cand_rows
        scenario = scenarios.get(sid)
        results.append(
            ScenarioResult(
                scenario_id=sid,
                title=scenario.title if scenario is not None else sid,
                tags=list(scenario.tags) if scenario is not None else [],
                trials=len(cand_rows),
                passes=passes,
                pass_rate=wilson_estimate(passes, len(cand_rows)) if cand_rows else None,
                baseline_pass_rate=(
                    wilson_estimate(sum(1 for r in base_rows if r.passed), len(base_rows))
                    if base_rows
                    else None
                ),
                flip=flip,
                cause=_dominant_cause(failing_side),
                flaky=flaky.get(sid),
            )
        )
    return results


def _group(rows: Sequence[TrialRow]) -> dict[str, list[TrialRow]]:
    grouped: dict[str, list[TrialRow]] = {}
    for row in rows:
        grouped.setdefault(row.scenario_id, []).append(row)
    return grouped


def _majority_passes(rows: Sequence[TrialRow]) -> bool:
    # Same rule as ``hecklr_stats.allocate_trials``: passing at least half the trials.
    return 2 * sum(1 for r in rows if r.passed) >= len(rows)


def _flip(cand: Sequence[TrialRow], base: Sequence[TrialRow]) -> FlipDirection | None:
    if not cand or not base:
        return None
    before, after = _majority_passes(base), _majority_passes(cand)
    if before and not after:
        return "pass_to_fail"
    if after and not before:
        return "fail_to_pass"
    return None


def _dominant_cause(rows: Sequence[TrialRow]) -> FailureCause | None:
    """The cause behind most failed trials of ``rows``, with its first rationale."""
    causes = [cause for row in rows if row.passed is False for cause in row.causes]
    if not causes:
        return None
    tally = Counter(cause.cause for cause in causes)
    top = min(tally, key=lambda name: (-tally[name], name))
    rationale = next((c.rationale for c in causes if c.cause == top and c.rationale), None)
    return FailureCause(cause=top, rationale=rationale)


_UNKNOWN_CAUSE = "no failing criterion recorded"


def _flip_groups(results: Sequence[ScenarioResult]) -> list[FlipGroup]:
    """Flipped scenarios grouped by direction and cause, biggest groups first (US-05)."""
    groups: dict[tuple[FlipDirection, str], list[ScenarioResult]] = {}
    for result in results:
        if result.flip is not None:
            cause = result.cause.cause if result.cause is not None else _UNKNOWN_CAUSE
            groups.setdefault((result.flip, cause), []).append(result)
    ordered = sorted(
        groups.items(),
        key=lambda item: (item[0][0] != "pass_to_fail", -len(item[1]), item[0][1]),
    )
    return [
        FlipGroup(
            direction=direction,
            cause=cause,
            scenario_ids=[m.scenario_id for m in members],
            rationale=next(
                (m.cause.rationale for m in members if m.cause and m.cause.rationale), None
            ),
        )
        for (direction, cause), members in ordered
    ]


def _voice(
    candidate: RunResults,
    cand: ConvertedRun,
    summaries: Sequence[MetricSummary],
    baseline_summaries: Sequence[MetricSummary],
    stats: StatsConfig,
    seed: int,
) -> VoiceSection | None:
    if not any(r.channel is Channel.VOICE and r.excluded != "harness_error" for r in cand.rows):
        return None
    events: dict[str, list[Event]] = {}
    for event in candidate.events:
        events.setdefault(event.trial_id, []).append(event)
    return VoiceSection(
        degradation=degradation(SNR_DB, SNR_ORDER, cand.outcomes, summaries, baseline_summaries),
        by_accent=degradation(ACCENT, None, cand.outcomes, summaries, baseline_summaries),
        metrics=voice_metrics(
            cand.rows,
            events,
            run_id=RunId(candidate.run.id),
            seed=seed,
            resamples=stats.bootstrap_resamples,
        ),
    )


def _trust(calibration: JudgeCalibration) -> JudgeTrust:
    return JudgeTrust(
        grader=calibration.grader,
        grader_version=calibration.grader_version,
        judge_model=calibration.judge_model,
        n=calibration.n,
        kappa=calibration.kappa,
        trusted=calibration.trusted,
        reason=calibration.untrusted_reason,
    )
