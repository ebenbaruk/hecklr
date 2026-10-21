"""Per-run aggregates as ``MetricSummary`` records (US-01, US-06).

One call turns a run's outcomes into every number the reports show for that run, each with its
interval: per-scenario pass rates, the suite pass rate overall and per slice, pass^k and pass@k,
and response-latency quantiles overall and per slice.
"""

from collections.abc import Sequence

import numpy as np

from hecklr_schemas import Estimate, MetricSummary, RunId, Slice
from hecklr_stats.compare import PASS_RATE
from hecklr_stats.intervals import DEFAULT_CONFIDENCE, derive_rng, quantile_estimates
from hecklr_stats.outcomes import (
    SCENARIO_DIMENSION,
    ScenarioCounts,
    TrialOutcome,
    slice_key,
    slices_of,
)
from hecklr_stats.rates import (
    scenario_pass_rate,
    suite_pass_at_k,
    suite_pass_hat_k,
    suite_pass_rate,
)

DEFAULT_KS = (3,)
"""pass^k and pass@k are reported for these k (the default suite runs 3 trials)."""

LATENCY_QUANTILES = {
    "response_latency_p50": 0.50,
    "response_latency_p95": 0.95,
    "response_latency_p99": 0.99,
}


def summarize_run(
    outcomes: Sequence[TrialOutcome],
    *,
    run_id: RunId,
    seed: int,
    resamples: int,
    confidence: float = DEFAULT_CONFIDENCE,
    ks: Sequence[int] = DEFAULT_KS,
) -> list[MetricSummary]:
    """Every per-run metric with its interval.

    Args:
        outcomes: The run's trial outcomes.
        run_id: The run they belong to.
        seed: Bootstrap seed; each (metric, slice) gets its own stream derived from it.
        resamples: Bootstrap replicates.
        confidence: Interval coverage.
        ks: k values for pass^k and pass@k; a k is skipped when no scenario has k trials.

    Returns:
        Suite metrics (slice ``{}``), then per-slice pass rates and latencies, then per-scenario
        pass rates (slice ``{"scenario": id}``).
    """
    if not outcomes:
        raise ValueError("cannot summarize a run with no outcomes")
    summaries: list[MetricSummary] = []

    def add(metric: str, slice_: Slice, estimate: Estimate) -> None:
        summaries.append(
            MetricSummary(run_id=run_id, metric=metric, slice=slice_, estimate=estimate)
        )

    counts = ScenarioCounts.from_outcomes(outcomes)
    for slice_ in [{}, *slices_of(outcomes)]:
        members = [o for o in outcomes if o.in_slice(slice_)]
        slice_counts = counts if not slice_ else ScenarioCounts.from_outcomes(members)
        rng_seed = _seed(seed, PASS_RATE, slice_)
        add(
            PASS_RATE,
            slice_,
            suite_pass_rate(
                slice_counts, seed=rng_seed, resamples=resamples, confidence=confidence
            ),
        )
        for metric, estimate in _latencies(members, seed, slice_, resamples, confidence):
            add(metric, slice_, estimate)
        if not slice_:
            for metric, estimate in _pass_k(counts, ks, seed, resamples, confidence):
                add(metric, slice_, estimate)

    for sid, n, c in zip(counts.scenario_ids, counts.trials, counts.passes, strict=True):
        add(PASS_RATE, {SCENARIO_DIMENSION: sid}, scenario_pass_rate(int(c), int(n), confidence))
    return summaries


def _pass_k(
    counts: ScenarioCounts, ks: Sequence[int], seed: int, resamples: int, confidence: float
) -> list[tuple[str, Estimate]]:
    results = []
    for k in ks:
        if int(counts.trials.max()) < k:
            continue
        for metric, estimator in ((f"pass^{k}", suite_pass_hat_k), (f"pass@{k}", suite_pass_at_k)):
            estimate = estimator(
                counts,
                k,
                seed=_seed(seed, metric, {}),
                resamples=resamples,
                confidence=confidence,
            )
            results.append((metric, estimate))
    return results


def _latencies(
    members: Sequence[TrialOutcome],
    seed: int,
    slice_: Slice,
    resamples: int,
    confidence: float,
) -> list[tuple[str, Estimate]]:
    """Latency quantiles over every agent turn, resampling whole trials."""
    values = [latency for outcome in members for latency in outcome.latencies_ms]
    if not values:
        return []
    groups = [i for i, outcome in enumerate(members) for _ in outcome.latencies_ms]
    estimates = quantile_estimates(
        values,
        list(LATENCY_QUANTILES.values()),
        seed=_seed(seed, "response_latency", slice_),
        resamples=resamples,
        groups=groups,
        confidence=confidence,
    )
    return list(zip(LATENCY_QUANTILES, estimates, strict=True))


def _seed(seed: int, metric: str, slice_: Slice) -> int:
    """An independent seed per (metric, slice), stable whatever else is summarized."""
    return int(derive_rng(seed, metric, slice_key(slice_)).integers(0, np.iinfo(np.int64).max))
