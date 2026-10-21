"""Response-latency comparisons between two runs (latency deltas in every report).

``hecklr_stats`` compares pass rates; ADR 0005b leaves latency comparisons to a later wave, and
the reports need them now. The rules (ADR 0007b):

- Each quantile (p50, p95, p99) of the agent's response latency is compared on the whole run and
  per channel (chat time to first token and voice response latency are different quantities).
- Each run is resampled by whole trials, independently: latencies are not paired between runs.
  Δ = candidate - baseline, with a percentile bootstrap interval at confidence ``1 - alpha``.
- The p-value inverts that interval: twice the smaller bootstrap tail mass of Δ on either side of
  0, so "p < alpha" and "the interval excludes 0" agree, as for pass rates.
- The minimum effect is relative: ``stats.min_effect`` times the baseline quantile (3% by
  default). A latency increase is the regression direction.
- Channel slices are Benjamini-Hochberg adjusted per quantile; the whole run is decided on its own
  p-value, like the suite pass rate.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np
import numpy.typing as npt

from hecklr_report.voice import QUANTILES, RESPONSE_LATENCY, quantile_metric
from hecklr_schemas import (
    Comparison,
    ComparisonId,
    Estimate,
    IntervalMethod,
    RunId,
    Slice,
    StatsConfig,
    new_id,
)
from hecklr_stats import TrialOutcome, benjamini_hochberg, decide_verdict
from hecklr_stats.intervals import chunk_sizes, derive_rng, percentile_interval
from hecklr_stats.outcomes import slice_key

FloatArray = npt.NDArray[np.float64]


def compare_latencies(
    baseline: Sequence[TrialOutcome],
    candidate: Sequence[TrialOutcome],
    *,
    baseline_run: RunId,
    candidate_run: RunId,
    config: StatsConfig,
    seed: int,
) -> list[Comparison]:
    """Latency quantile comparisons on the whole run and per channel, when both runs have data."""
    channels = sorted(
        {o.labels["channel"] for o in [*baseline, *candidate] if "channel" in o.labels}
    )
    slices: list[Slice] = [{}, *({"channel": c} for c in channels)]
    raw: list[list[tuple[Slice, _Result]]] = [[] for _ in QUANTILES]
    for slice_ in slices:
        base = [o for o in baseline if o.in_slice(slice_) and o.latencies_ms]
        cand = [o for o in candidate if o.in_slice(slice_) and o.latencies_ms]
        if not base or not cand:
            continue
        rng = derive_rng(seed, RESPONSE_LATENCY, slice_key(slice_))
        per_quantile = _compare(base, cand, rng, config.bootstrap_resamples, 1 - config.alpha)
        for column, result in enumerate(per_quantile):
            raw[column].append((slice_, result))

    created_at = datetime.now(UTC)
    comparisons = []
    for q_name, results in zip(QUANTILES, raw, strict=True):
        if not results:
            continue
        has_suite = not results[0][0]
        family = results[1:] if has_suite else results
        q_values = benjamini_hochberg([r.p_value for _, r in family]).tolist()
        if has_suite:
            q_values = [results[0][1].p_value, *q_values]
        for (slice_, result), q_value in zip(results, q_values, strict=True):
            comparisons.append(
                _comparison(
                    result,
                    metric=quantile_metric(RESPONSE_LATENCY, q_name),
                    slice_=slice_,
                    q_value=q_value,
                    baseline_run=baseline_run,
                    candidate_run=candidate_run,
                    config=config,
                    created_at=created_at,
                )
            )
    return comparisons


@dataclass(frozen=True, slots=True)
class _Result:
    """One quantile's estimates for both runs and their difference."""

    baseline: Estimate
    candidate: Estimate
    delta: Estimate
    p_value: float
    n_base: int
    n_cand: int
    n_scenarios: int


def _compare(
    base: Sequence[TrialOutcome],
    cand: Sequence[TrialOutcome],
    rng: np.random.Generator,
    resamples: int,
    confidence: float,
) -> list[_Result]:
    probs = np.array(list(QUANTILES.values()))
    base_points, base_reps = _quantiles(base, probs, rng, resamples)
    cand_points, cand_reps = _quantiles(cand, probs, rng, resamples)
    shared = len({o.scenario_id for o in base} & {o.scenario_id for o in cand})
    results = []
    for column in range(probs.size):
        delta_reps = cand_reps[:, column] - base_reps[:, column]
        lower = (1 + np.count_nonzero(delta_reps <= 0)) / (1 + resamples)
        upper = (1 + np.count_nonzero(delta_reps >= 0)) / (1 + resamples)
        results.append(
            _Result(
                _estimate(base_points[column], base_reps[:, column], len(base), confidence),
                _estimate(cand_points[column], cand_reps[:, column], len(cand), confidence),
                _estimate(
                    cand_points[column] - base_points[column], delta_reps, shared, confidence
                ),
                min(1.0, 2 * min(lower, upper)),
                len(base),
                len(cand),
                shared,
            )
        )
    return results


def _quantiles(
    outcomes: Sequence[TrialOutcome], probs: FloatArray, rng: np.random.Generator, resamples: int
) -> tuple[FloatArray, FloatArray]:
    """Inverted-CDF quantiles and their trial-bootstrap replicates, shape (resamples, quantiles).

    A replicate draws trials with replacement; a latency's weight is how often its trial was
    drawn, and a quantile is the first sorted latency whose cumulative weight reaches q x total.
    """
    values = np.array([v for o in outcomes for v in o.latencies_ms], dtype=np.float64)
    groups = np.array([i for i, o in enumerate(outcomes) for _ in o.latencies_ms])
    order = np.argsort(values, kind="stable")
    sorted_values, sorted_groups = values[order], groups[order]
    n_groups = len(outcomes)
    chunks = []
    for size in chunk_sizes(resamples, values.size):
        draws = rng.multinomial(n_groups, np.full(n_groups, 1.0 / n_groups), size=size)
        cumulative = np.cumsum(draws[:, sorted_groups], axis=1)
        targets = np.maximum(probs[None, :] * cumulative[:, -1:], 1)
        positions = np.stack(
            [np.argmax(cumulative >= targets[:, [c]], axis=1) for c in range(probs.size)], axis=1
        )
        chunks.append(sorted_values[positions])
    points = np.quantile(values, probs, method="inverted_cdf")
    return points, np.concatenate(chunks)


def _estimate(point: float, replicates: FloatArray, n: int, confidence: float) -> Estimate:
    low, high = percentile_interval(replicates, confidence)
    value = float(point)
    return Estimate(
        value=value,
        ci_low=min(low, value),
        ci_high=max(high, value),
        confidence=confidence,
        n=n,
        method=IntervalMethod.BOOTSTRAP,
    )


def _comparison(
    result: _Result,
    *,
    metric: str,
    slice_: Slice,
    q_value: float,
    baseline_run: RunId,
    candidate_run: RunId,
    config: StatsConfig,
    created_at: datetime,
) -> Comparison:
    min_effect_ms = config.min_effect * result.baseline.value
    # The verdict rule calls a negative change a regression; for latency an increase is one, so
    # the rule sees the improvement (baseline - candidate) instead of the delta.
    improvement = Estimate(
        value=-result.delta.value,
        ci_low=-result.delta.ci_high,
        ci_high=-result.delta.ci_low,
        confidence=result.delta.confidence,
        n=result.delta.n,
        method=result.delta.method,
    )
    tested = q_value if slice_ else result.p_value
    return Comparison(
        id=ComparisonId(new_id()),
        baseline=baseline_run,
        candidate=candidate_run,
        metric=metric,
        slice=slice_,
        baseline_estimate=result.baseline,
        candidate_estimate=result.candidate,
        delta=result.delta,
        p_value=result.p_value,
        q_value=q_value,
        verdict=decide_verdict(improvement, tested, config.alpha, min_effect_ms),
        alpha=config.alpha,
        min_effect=min_effect_ms,
        n_scenarios=result.n_scenarios,
        n_trials_baseline=result.n_base,
        n_trials_candidate=result.n_cand,
        created_at=created_at,
    )
