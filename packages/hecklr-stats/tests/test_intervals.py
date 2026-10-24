import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from scipy.stats import binom

from hecklr_schemas import IntervalMethod
from hecklr_stats.intervals import (
    chunk_sizes,
    derive_rng,
    paired_difference_interval,
    quantile_estimates,
    wilson_estimate,
    wilson_interval,
    z_value,
)


@pytest.mark.parametrize(
    ("passes", "trials", "low", "high"),
    [
        # Reference values from Newcombe (1998), Table I, and R's prop.test(correct=FALSE).
        (0, 10, 0.0, 0.2775),
        (5, 10, 0.2366, 0.7634),
        (10, 10, 0.7225, 1.0),
        (81, 263, 0.2553, 0.3662),
    ],
)
def test_wilson_matches_reference_values(passes: int, trials: int, low: float, high: float) -> None:
    got_low, got_high = wilson_interval(passes, trials)
    assert got_low == pytest.approx(low, abs=5e-4)
    assert got_high == pytest.approx(high, abs=5e-4)


@given(trials=st.integers(1, 500), data=st.data())
def test_wilson_is_ordered_bounded_and_contains_the_rate(trials: int, data: st.DataObject) -> None:
    passes = data.draw(st.integers(0, trials))
    low, high = wilson_interval(passes, trials)
    assert 0.0 <= low <= passes / trials <= high <= 1.0


@given(passes=st.integers(0, 50), trials=st.integers(50, 200))
def test_wilson_widens_with_confidence(passes: int, trials: int) -> None:
    narrow = wilson_interval(passes, trials, 0.80)
    wide = wilson_interval(passes, trials, 0.99)
    assert wide[0] <= narrow[0]
    assert narrow[1] <= wide[1]


def test_wilson_coverage_averages_to_nominal() -> None:
    # Exact coverage (no simulation): sum the binomial pmf over outcomes whose interval covers p.
    trials = 20
    outcomes = np.arange(trials + 1)
    bounds = np.array([wilson_interval(int(c), trials) for c in outcomes])
    coverages = []
    for p in np.linspace(0.05, 0.95, 91):
        covered = (bounds[:, 0] <= p) & (p <= bounds[:, 1])
        coverages.append(binom.pmf(outcomes, trials, p)[covered].sum())
    assert np.mean(coverages) == pytest.approx(0.95, abs=0.015)


def test_wilson_estimate_carries_method_and_n() -> None:
    estimate = wilson_estimate(3, 4)
    assert estimate.value == 0.75
    assert estimate.n == 4
    assert estimate.method is IntervalMethod.WILSON


@pytest.mark.parametrize(("passes", "trials"), [(0, 0), (-1, 3), (4, 3)])
def test_wilson_rejects_invalid_counts(passes: int, trials: int) -> None:
    with pytest.raises(ValueError, match="trials"):
        wilson_interval(passes, trials)


def test_z_value_rejects_out_of_range_confidence() -> None:
    assert z_value(0.95) == pytest.approx(1.959964, abs=1e-6)
    with pytest.raises(ValueError, match="confidence"):
        z_value(1.0)


def test_derive_rng_is_stable_and_key_dependent() -> None:
    first = derive_rng(7, "tag=billing").integers(0, 1_000_000, 5)
    again = derive_rng(7, "tag=billing").integers(0, 1_000_000, 5)
    other = derive_rng(7, "tag=scheduling").integers(0, 1_000_000, 5)
    negative_seed = derive_rng(-7, "tag=billing").integers(0, 1_000_000, 5)
    assert np.array_equal(first, again)
    assert not np.array_equal(first, other)
    assert not np.array_equal(first, negative_seed)


def test_chunk_sizes_cover_all_resamples() -> None:
    assert sum(chunk_sizes(10_000, 2_000)) == 10_000
    assert len(list(chunk_sizes(10_000, 2_000))) > 1
    with pytest.raises(ValueError, match="resamples"):
        list(chunk_sizes(0, 10))


def test_us06_latency_quantiles_have_bootstrap_intervals() -> None:
    rng = np.random.default_rng(0)
    latencies = rng.lognormal(mean=6.5, sigma=0.4, size=2_000)
    p50, p95, p99 = quantile_estimates(latencies, (0.5, 0.95, 0.99), seed=1, resamples=2_000)
    for estimate, q in ((p50, 0.5), (p95, 0.95), (p99, 0.99)):
        assert estimate.value == pytest.approx(np.quantile(latencies, q, method="inverted_cdf"))
        assert estimate.ci_low <= estimate.value <= estimate.ci_high
        assert estimate.method is IntervalMethod.BOOTSTRAP
        assert estimate.n == 2_000
    assert p50.value < p95.value < p99.value
    assert p99.ci_high - p99.ci_low > p50.ci_high - p50.ci_low


def test_latency_quantile_interval_covers_the_true_quantile() -> None:
    # Lognormal median is exp(mu); check the bootstrap interval covers it about 95% of the time.
    true_median = float(np.exp(6.0))
    hits = 0
    for replicate in range(200):
        sample = np.random.default_rng(replicate).lognormal(6.0, 0.5, size=300)
        (median,) = quantile_estimates(sample, (0.5,), seed=replicate, resamples=1_000)
        hits += median.ci_low <= true_median <= median.ci_high
    assert 0.90 <= hits / 200 <= 0.99


def test_grouped_quantiles_count_groups_and_widen_the_interval() -> None:
    # Ten trials whose turns share a trial-level offset: turns are not independent.
    rng = np.random.default_rng(3)
    offsets = rng.normal(0, 300, size=10)
    groups = np.repeat(np.arange(10), 50)
    values = 800 + offsets[groups] + rng.normal(0, 20, size=500)
    (iid,) = quantile_estimates(values, (0.5,), seed=4, resamples=2_000)
    (clustered,) = quantile_estimates(values, (0.5,), seed=4, resamples=2_000, groups=groups)
    assert clustered.n == 10
    assert clustered.ci_high - clustered.ci_low > 2 * (iid.ci_high - iid.ci_low)


def test_quantile_estimates_are_reproducible_from_the_seed() -> None:
    values = list(range(100))
    assert quantile_estimates(values, (0.9,), seed=5, resamples=500) == quantile_estimates(
        values, (0.9,), seed=5, resamples=500
    )


@pytest.mark.parametrize(
    ("values", "quantiles", "groups", "message"),
    [
        ([], (0.5,), None, "no values"),
        ([1.0], (1.5,), None, r"\[0, 1\]"),
        ([1.0, 2.0], (0.5,), [0], "one entry per value"),
    ],
)
def test_quantile_estimates_reject_bad_input(
    values: list[float], quantiles: tuple[float, ...], groups: list[int] | None, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        quantile_estimates(values, quantiles, seed=0, resamples=10, groups=groups)


def test_quantile_of_extremes_are_min_and_max() -> None:
    low, high = quantile_estimates([5.0, 1.0, 3.0], (0.0, 1.0), seed=0, resamples=100)
    assert (low.value, high.value) == (1.0, 5.0)
    assert low.ci_low >= 1.0
    assert high.ci_high <= 5.0


# --- paired difference of proportions (issue #98) ----------------------------------------


def test_issue98_paired_interval_without_discordant_pairs_is_the_wilson_bound() -> None:
    for pairs in (1, 3, 40, 400):
        bound = wilson_interval(0, pairs)[1]
        low, high = paired_difference_interval(0, 0, pairs)
        assert (low, high) == pytest.approx((-bound, bound))


@given(
    st.integers(min_value=1, max_value=200).flatmap(
        lambda n: st.tuples(
            st.just(n),
            st.integers(0, n).flatmap(lambda g: st.tuples(st.just(g), st.integers(0, n - g))),
        )
    )
)
def test_issue98_paired_interval_contains_the_difference_and_is_antisymmetric(
    case: tuple[int, tuple[int, int]],
) -> None:
    pairs, (gained, lost) = case
    low, high = paired_difference_interval(gained, lost, pairs)
    assert -1.0 <= low <= (gained - lost) / pairs <= high <= 1.0
    assert low < high
    assert paired_difference_interval(lost, gained, pairs) == pytest.approx((-high, -low))


@pytest.mark.parametrize("pairs", [10, 40, 150])
@pytest.mark.parametrize(
    "cells", [(0.8, 0.05, 0.05, 0.1), (0.9, 0.0, 0.1, 0.0), (0.6, 0.2, 0.0, 0.2), (1, 0, 0, 0)]
)
def test_issue98_paired_interval_covers_at_nominal_rate(
    pairs: int, cells: tuple[float, float, float, float]
) -> None:
    rng = np.random.default_rng(pairs)
    true = cells[1] - cells[2]
    draws = rng.multinomial(pairs, cells, size=2_000)
    covered = 0
    for _, gained, lost, _ in draws:
        low, high = paired_difference_interval(int(gained), int(lost), pairs)
        covered += low - 1e-12 <= true <= high + 1e-12
    assert covered / len(draws) >= 0.94


def test_issue98_paired_interval_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError, match="pairs"):
        paired_difference_interval(3, 2, 4)
    with pytest.raises(ValueError, match="pairs"):
        paired_difference_interval(0, 0, 0)
