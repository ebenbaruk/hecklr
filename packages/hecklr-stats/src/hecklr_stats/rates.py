"""Pass rate, pass^k and pass@k, per scenario and for the suite (spec, "Metrics", "Intervals").

Suite metrics are the unweighted mean over scenarios, with a two-stage cluster bootstrap:
resample scenarios with replacement, then resample the trials of each drawn scenario. For
pass/fail trials the second stage is exactly a binomial draw at the scenario's observed rate,
which is what makes the whole bootstrap a handful of vectorized numpy calls.
"""

from collections.abc import Callable

import numpy as np

from hecklr_schemas import Estimate, IntervalMethod
from hecklr_stats.intervals import (
    DEFAULT_CONFIDENCE,
    chunk_sizes,
    percentile_interval,
    wilson_interval,
)
from hecklr_stats.outcomes import FloatArray, IntArray, ScenarioCounts

ScenarioStatistic = Callable[[IntArray, IntArray], FloatArray]
"""Per-scenario estimator: (trials, passes) arrays in, one value per element out."""


def pass_hat_k(trials: IntArray, passes: IntArray, k: int) -> FloatArray:
    """Unbiased pass^k: probability that k trials drawn without replacement all pass.

    ``C(c, k) / C(n, k)`` written as a product, which is exact for the small n of a run and is
    zero whenever fewer than k trials passed. Requires ``n >= k``.
    """
    _check_k(trials, k)
    steps = np.arange(k)
    return np.prod((passes[..., None] - steps) / (trials[..., None] - steps), axis=-1).clip(0, 1)


def pass_at_k(trials: IntArray, passes: IntArray, k: int) -> FloatArray:
    """Unbiased pass@k: probability that at least one of k trials passes.

    ``1 - C(n-c, k) / C(n, k)``: one minus pass^k of the failures.
    """
    return 1.0 - pass_hat_k(trials, trials - passes, k)


def pass_rate(trials: IntArray, passes: IntArray) -> FloatArray:
    """Observed pass rate."""
    return passes / trials


def _check_k(trials: IntArray, k: int) -> None:
    if k < 1:
        raise ValueError(f"k must be >= 1, got {k}")
    if np.any(trials < k):
        raise ValueError(f"pass^{k} and pass@{k} need at least {k} trials per scenario")


def scenario_pass_rate(
    passes: int, trials: int, confidence: float = DEFAULT_CONFIDENCE
) -> Estimate:
    """A scenario's pass rate with its Wilson interval."""
    low, high = wilson_interval(passes, trials, confidence)
    return _estimate(passes / trials, low, high, trials, confidence, IntervalMethod.WILSON)


def scenario_pass_hat_k(
    passes: int, trials: int, k: int, confidence: float = DEFAULT_CONFIDENCE
) -> Estimate:
    """A scenario's pass^k with the Wilson interval mapped through ``p -> p^k``.

    The map is monotone, so the image of a valid interval for p is a valid interval for p^k.
    """
    value = float(pass_hat_k(np.array(trials), np.array(passes), k))
    low, high = wilson_interval(passes, trials, confidence)
    return _estimate(value, low**k, high**k, trials, confidence, IntervalMethod.WILSON)


def scenario_pass_at_k(
    passes: int, trials: int, k: int, confidence: float = DEFAULT_CONFIDENCE
) -> Estimate:
    """A scenario's pass@k with the Wilson interval mapped through ``p -> 1 - (1-p)^k``."""
    value = float(pass_at_k(np.array(trials), np.array(passes), k))
    low, high = wilson_interval(passes, trials, confidence)
    return _estimate(
        value, 1 - (1 - low) ** k, 1 - (1 - high) ** k, trials, confidence, IntervalMethod.WILSON
    )


def _estimate(
    value: float, low: float, high: float, n: int, confidence: float, method: IntervalMethod
) -> Estimate:
    # The unbiased point estimate of p^k is not the plug-in (c/n)^k, so it can sit a hair outside
    # the mapped interval; the interval is widened to contain it rather than report a contradiction.
    return Estimate(
        value=value,
        ci_low=min(low, value),
        ci_high=max(high, value),
        confidence=confidence,
        n=n,
        method=method,
    )


def suite_estimate(
    counts: ScenarioCounts,
    statistic: ScenarioStatistic,
    *,
    seed: int,
    resamples: int,
    confidence: float = DEFAULT_CONFIDENCE,
) -> Estimate:
    """Mean of a per-scenario statistic over scenarios, with a cluster bootstrap interval."""
    if counts.n_scenarios == 0:
        raise ValueError("cannot estimate a suite metric over no scenarios")
    point = float(np.mean(statistic(counts.trials, counts.passes)))
    replicates = cluster_bootstrap(counts, statistic, np.random.default_rng(seed), resamples)
    # The second stage draws c* ~ Binomial(n, c/n), and a nonlinear statistic of c* is biased
    # (pass^k of the draws centers on (c/n)^k, above the unbiased estimate). Recentring the
    # replicates on the point estimate removes that bias; pass^3 coverage moves from ~0.89 to ~0.95.
    replicates = replicates + (point - replicates.mean())
    low, high = percentile_interval(replicates, confidence)
    return Estimate(
        value=point,
        ci_low=max(0.0, min(low, point)),
        ci_high=min(1.0, max(high, point)),
        confidence=confidence,
        n=counts.n_scenarios,
        method=IntervalMethod.CLUSTER_BOOTSTRAP,
    )


def cluster_bootstrap(
    counts: ScenarioCounts,
    statistic: ScenarioStatistic,
    rng: np.random.Generator,
    resamples: int,
) -> FloatArray:
    """``resamples`` replicates of the suite mean of ``statistic`` (scenarios, then trials)."""
    table = _statistic_table(counts, statistic)
    replicates = []
    for size in chunk_sizes(resamples, counts.n_scenarios):
        drawn = rng.integers(0, counts.n_scenarios, size=(size, counts.n_scenarios))
        passes = rng.binomial(counts.trials[drawn], counts.rates[drawn])
        replicates.append(table[drawn, passes].mean(axis=1))
    return np.concatenate(replicates)


def _statistic_table(counts: ScenarioCounts, statistic: ScenarioStatistic) -> FloatArray:
    """``table[s, c]`` = the statistic of scenario s had c of its trials passed.

    Precomputing every possible pass count turns each bootstrap replicate into a gather.
    """
    possible = np.arange(int(counts.trials.max()) + 1)
    trials = np.broadcast_to(counts.trials[:, None], (counts.n_scenarios, possible.size))
    passes = np.minimum(possible[None, :], trials)
    return statistic(trials, passes)


def suite_pass_rate(
    counts: ScenarioCounts, *, seed: int, resamples: int, confidence: float = DEFAULT_CONFIDENCE
) -> Estimate:
    """Suite pass rate: mean of per-scenario pass rates, each scenario weighted equally."""
    return suite_estimate(counts, pass_rate, seed=seed, resamples=resamples, confidence=confidence)


def suite_pass_hat_k(
    counts: ScenarioCounts,
    k: int,
    *,
    seed: int,
    resamples: int,
    confidence: float = DEFAULT_CONFIDENCE,
) -> Estimate:
    """Suite pass^k over the scenarios that have at least k trials."""
    eligible = counts.select(_with_at_least(counts, k))
    return suite_estimate(
        eligible,
        lambda n, c: pass_hat_k(n, c, k),
        seed=seed,
        resamples=resamples,
        confidence=confidence,
    )


def suite_pass_at_k(
    counts: ScenarioCounts,
    k: int,
    *,
    seed: int,
    resamples: int,
    confidence: float = DEFAULT_CONFIDENCE,
) -> Estimate:
    """Suite pass@k over the scenarios that have at least k trials."""
    eligible = counts.select(_with_at_least(counts, k))
    return suite_estimate(
        eligible,
        lambda n, c: pass_at_k(n, c, k),
        seed=seed,
        resamples=resamples,
        confidence=confidence,
    )


def _with_at_least(counts: ScenarioCounts, k: int) -> list[str]:
    return [sid for sid, n in zip(counts.scenario_ids, counts.trials, strict=True) if n >= k]
