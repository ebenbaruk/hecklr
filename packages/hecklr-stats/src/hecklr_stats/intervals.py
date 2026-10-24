"""Confidence intervals: Wilson score, bootstrap percentile, and quantiles of latencies."""

import hashlib
import math
from collections.abc import Iterator, Sequence

import numpy as np
import numpy.typing as npt
from scipy.stats import norm

from hecklr_schemas import Estimate, IntervalMethod
from hecklr_stats.outcomes import FloatArray

DEFAULT_CONFIDENCE = 0.95

_MAX_CHUNK_ELEMENTS = 4_000_000
"""Bootstrap matrices are built in chunks of at most this many cells (32 MB of float64), so a
large suite does not allocate resamples x scenarios all at once."""


def z_value(confidence: float) -> float:
    """Two-sided standard normal critical value, e.g. 1.96 for 0.95."""
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
    return float(norm.ppf(0.5 + confidence / 2))


def wilson_interval(
    passes: int, trials: int, confidence: float = DEFAULT_CONFIDENCE
) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (correct near 0, 1 and at small n)."""
    if trials < 1 or not 0 <= passes <= trials:
        raise ValueError(f"need trials >= 1 and 0 <= passes <= trials, got {passes}/{trials}")
    z = z_value(confidence)
    z2 = z * z
    center = (passes + z2 / 2) / (trials + z2)
    half = z / (trials + z2) * math.sqrt(passes * (trials - passes) / trials + z2 / 4)
    # At 0 or n passes one bound is exactly 0 or 1; computing it would leave a rounding residue.
    low = 0.0 if passes == 0 else max(0.0, center - half)
    high = 1.0 if passes == trials else min(1.0, center + half)
    return low, high


def wilson_estimate(passes: int, trials: int, confidence: float = DEFAULT_CONFIDENCE) -> Estimate:
    """A proportion with its Wilson interval."""
    low, high = wilson_interval(passes, trials, confidence)
    return Estimate(
        value=passes / trials,
        ci_low=low,
        ci_high=high,
        confidence=confidence,
        n=trials,
        method=IntervalMethod.WILSON,
    )


def paired_difference_interval(
    gained: int, lost: int, pairs: int, confidence: float = DEFAULT_CONFIDENCE
) -> tuple[float, float]:
    """Interval for a difference of paired proportions, from its two discordant counts.

    With ``pairs`` paired pass/fail outcomes, the difference is ``(gained - lost) / pairs``, where
    ``gained`` pairs passed only in the second sample and ``lost`` only in the first. Each
    discordant share gets its Wilson interval and the two are combined by MOVER ("method of
    variance estimates recovery"), with the multinomial correlation of the two shares. Unlike a
    bootstrap, it never collapses to zero width: with no discordant pair the bound is the Wilson
    bound of ``0 / pairs``.
    """
    if pairs < 1 or gained < 0 or lost < 0 or gained + lost > pairs:
        raise ValueError(f"need 0 <= gained + lost <= pairs, got {gained}, {lost} of {pairs}")
    gain, loss = gained / pairs, lost / pairs
    gain_low, gain_high = wilson_interval(gained, pairs, confidence)
    loss_low, loss_high = wilson_interval(lost, pairs, confidence)
    corr = 0.0
    if 0 < gain < 1 and 0 < loss < 1:
        corr = -math.sqrt(gain * loss / ((1 - gain) * (1 - loss)))
    point = gain - loss
    down = _mover_term(gain - gain_low, loss_high - loss, corr)
    up = _mover_term(gain_high - gain, loss - loss_low, corr)
    return max(-1.0, point - down), min(1.0, point + up)


def _mover_term(first: float, second: float, corr: float) -> float:
    # Rounding can push the radicand a hair below zero when the two terms nearly cancel.
    return math.sqrt(max(0.0, first * first + second * second - 2 * corr * first * second))


def derive_rng(seed: int, *keys: str) -> np.random.Generator:
    """A generator seeded by ``seed`` and ``keys``, independent of call order.

    Slices of one comparison get their own stream from (run seed, slice key), so adding a slice
    never changes another slice's numbers. ``hashlib`` keeps it stable across processes.
    """
    words = [int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "little") for key in keys]
    return np.random.default_rng(np.random.SeedSequence([seed % 2**64, *words]))


def chunk_sizes(resamples: int, width: int) -> Iterator[int]:
    """Split ``resamples`` rows of ``width`` cells into memory-bounded chunks."""
    if resamples < 1:
        raise ValueError(f"resamples must be >= 1, got {resamples}")
    step = max(1, _MAX_CHUNK_ELEMENTS // max(1, width))
    for start in range(0, resamples, step):
        yield min(step, resamples - start)


def percentile_interval(
    replicates: npt.NDArray[np.float64], confidence: float
) -> tuple[float, float]:
    """Percentile bootstrap interval of ``replicates``."""
    tail = (1 - confidence) / 2
    low, high = np.quantile(replicates, [tail, 1 - tail])
    return float(low), float(high)


def quantile_estimates(
    values: Sequence[float] | FloatArray,
    quantiles: Sequence[float],
    *,
    seed: int,
    resamples: int,
    groups: Sequence[int] | npt.NDArray[np.int64] | None = None,
    confidence: float = DEFAULT_CONFIDENCE,
) -> list[Estimate]:
    """Quantiles of ``values`` with bootstrap intervals on the quantile itself.

    Quantiles use the inverted-CDF definition (an observed value, never an interpolation), so the
    point estimate and every bootstrap replicate are computed the same way. With ``groups``, whole
    groups are resampled together: latencies of one trial are correlated, so the trial, not the
    turn, is the independent unit (``Estimate.n`` counts groups).

    Args:
        values: Observations, e.g. response latencies in ms.
        quantiles: Probabilities in [0, 1], e.g. ``(0.5, 0.95, 0.99)``.
        seed: Bootstrap seed.
        resamples: Bootstrap replicates.
        groups: Optional group id per value (e.g. the trial index); ids need not be contiguous.
        confidence: Interval coverage.
    """
    data = np.asarray(values, dtype=np.float64)
    probs = np.asarray(quantiles, dtype=np.float64)
    if data.size == 0:
        raise ValueError("cannot estimate quantiles of no values")
    if np.any((probs < 0) | (probs > 1)):
        raise ValueError("quantiles must be in [0, 1]")
    raw_groups = np.arange(data.size) if groups is None else np.asarray(groups)
    if raw_groups.shape != data.shape:
        raise ValueError("groups must have one entry per value")
    order = np.argsort(data, kind="stable")
    sorted_values = data[order]
    _, group_index = np.unique(raw_groups[order], return_inverse=True)
    n_groups = int(group_index.max()) + 1

    rng = np.random.default_rng(seed)
    replicates = np.concatenate(
        [
            _weighted_quantiles(sorted_values, group_index, n_groups, probs, rng, size)
            for size in chunk_sizes(resamples, data.size)
        ]
    )
    points = np.quantile(data, probs, method="inverted_cdf")
    estimates = []
    for column, point in enumerate(points):
        low, high = percentile_interval(replicates[:, column], confidence)
        # A percentile interval of a discrete statistic can miss its own point estimate by one
        # observation; widening to include it keeps "value inside its interval" true in reports.
        estimates.append(
            Estimate(
                value=float(point),
                ci_low=min(low, float(point)),
                ci_high=max(high, float(point)),
                confidence=confidence,
                n=n_groups,
                method=IntervalMethod.BOOTSTRAP,
            )
        )
    return estimates


def _weighted_quantiles(
    sorted_values: FloatArray,
    group_index: npt.NDArray[np.intp],
    n_groups: int,
    probs: FloatArray,
    rng: np.random.Generator,
    size: int,
) -> FloatArray:
    """Inverted-CDF quantiles of ``size`` group-bootstrap replicates, shape (size, len(probs)).

    A replicate draws groups with replacement; a value's weight is how often its group was drawn.
    The quantile is the first sorted value whose cumulative weight reaches ``q * total``.
    """
    draws = rng.multinomial(n_groups, np.full(n_groups, 1.0 / n_groups), size=size)
    cumulative = np.cumsum(draws[:, group_index], axis=1)
    targets = probs[None, :] * cumulative[:, -1:]
    positions = np.empty((size, probs.size), dtype=np.intp)
    for column in range(probs.size):
        reached = cumulative >= np.maximum(targets[:, column : column + 1], 1)
        positions[:, column] = np.argmax(reached, axis=1)
    return sorted_values[positions]
