from itertools import combinations

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from hecklr_schemas import IntervalMethod
from hecklr_stats.outcomes import ScenarioCounts, TrialOutcome
from hecklr_stats.rates import (
    pass_at_k,
    pass_hat_k,
    scenario_pass_at_k,
    scenario_pass_hat_k,
    scenario_pass_rate,
    suite_pass_at_k,
    suite_pass_hat_k,
    suite_pass_rate,
)


def _brute_force(trials: int, passes: int, k: int) -> tuple[float, float]:
    """pass^k and pass@k by enumerating every k-subset of the trials."""
    outcomes = [True] * passes + [False] * (trials - passes)
    subsets = list(combinations(outcomes, k))
    return (
        sum(all(s) for s in subsets) / len(subsets),
        sum(any(s) for s in subsets) / len(subsets),
    )


@given(trials=st.integers(1, 10), data=st.data())
def test_pass_k_estimators_match_subset_enumeration(trials: int, data: st.DataObject) -> None:
    passes = data.draw(st.integers(0, trials))
    k = data.draw(st.integers(1, trials))
    hat, at = _brute_force(trials, passes, k)
    assert float(pass_hat_k(np.array(trials), np.array(passes), k)) == pytest.approx(hat)
    assert float(pass_at_k(np.array(trials), np.array(passes), k)) == pytest.approx(at)


def test_pass_hat_k_is_the_reliability_customers_feel() -> None:
    # Spec: "a 90% pass rate can mean 59% pass^5"; the unbiased estimator on 9/10 agrees.
    assert float(pass_hat_k(np.array(10), np.array(9), 5)) == pytest.approx(0.5)
    assert float(pass_hat_k(np.array(1000), np.array(900), 5)) == pytest.approx(0.59, abs=0.005)


def test_pass_k_needs_enough_trials() -> None:
    with pytest.raises(ValueError, match="at least 3 trials"):
        pass_hat_k(np.array([2]), np.array([1]), 3)
    with pytest.raises(ValueError, match="k must be"):
        pass_at_k(np.array([2]), np.array([1]), 0)


def test_us01_scenario_pass_rate_uses_wilson() -> None:
    estimate = scenario_pass_rate(7, 10)
    assert estimate.value == 0.7
    assert estimate.method is IntervalMethod.WILSON
    assert 0.39 < estimate.ci_low < estimate.ci_high < 0.90


@given(trials=st.integers(3, 40), data=st.data())
def test_scenario_pass_k_intervals_contain_their_estimates(
    trials: int, data: st.DataObject
) -> None:
    passes = data.draw(st.integers(0, trials))
    k = data.draw(st.integers(1, 3))
    for estimate in (scenario_pass_hat_k(passes, trials, k), scenario_pass_at_k(passes, trials, k)):
        assert 0.0 <= estimate.ci_low <= estimate.value <= estimate.ci_high <= 1.0


def _counts(rates: list[float], trials: int) -> ScenarioCounts:
    passes = [round(r * trials) for r in rates]
    return ScenarioCounts.from_arrays([trials] * len(rates), passes)


def test_us01_suite_pass_rate_weights_scenarios_equally() -> None:
    # One scenario with 100 trials must not dominate one with 4.
    counts = ScenarioCounts.from_arrays([100, 4], [100, 0])
    estimate = suite_pass_rate(counts, seed=0, resamples=2_000)
    assert estimate.value == 0.5
    assert estimate.n == 2
    assert estimate.method is IntervalMethod.CLUSTER_BOOTSTRAP
    assert estimate.ci_low <= 0.5 <= estimate.ci_high


def test_suite_pass_rate_is_reproducible_and_seed_dependent() -> None:
    counts = _counts([0.2, 0.5, 0.9, 1.0, 0.7], 10)
    first = suite_pass_rate(counts, seed=1, resamples=1_000)
    assert first == suite_pass_rate(counts, seed=1, resamples=1_000)
    assert first != suite_pass_rate(counts, seed=2, resamples=1_000)


def test_all_passing_suite_has_a_degenerate_interval() -> None:
    estimate = suite_pass_rate(_counts([1.0] * 5, 3), seed=0, resamples=500)
    assert (estimate.value, estimate.ci_low, estimate.ci_high) == (1.0, 1.0, 1.0)


def test_suite_interval_widens_with_scenario_heterogeneity() -> None:
    uniform = suite_pass_rate(_counts([0.5] * 40, 10), seed=0, resamples=2_000)
    spread = suite_pass_rate(_counts([0.0, 1.0] * 20, 10), seed=0, resamples=2_000)
    assert spread.ci_high - spread.ci_low > 1.5 * (uniform.ci_high - uniform.ci_low)


def test_suite_pass_k_uses_scenarios_with_enough_trials() -> None:
    counts = ScenarioCounts.from_arrays([5, 5, 2], [5, 4, 2])
    hat = suite_pass_hat_k(counts, 3, seed=0, resamples=500)
    at = suite_pass_at_k(counts, 3, seed=0, resamples=500)
    assert hat.n == 2
    assert hat.value == pytest.approx((1.0 + 0.4) / 2)
    assert at.value == pytest.approx(1.0)
    assert hat.ci_low <= hat.value <= hat.ci_high
    with pytest.raises(ValueError, match="no scenarios"):
        suite_pass_hat_k(counts, 6, seed=0, resamples=10)


def test_scenario_counts_from_outcomes_sorts_and_tallies() -> None:
    outcomes = [
        TrialOutcome(scenario_id="b", passed=True),
        TrialOutcome(scenario_id="a", passed=False),
        TrialOutcome(scenario_id="b", passed=False),
    ]
    counts = ScenarioCounts.from_outcomes(outcomes)
    assert counts.scenario_ids == ("a", "b")
    assert counts.trials.tolist() == [1, 2]
    assert counts.passes.tolist() == [0, 1]
    assert counts.select(["b"]).scenario_ids == ("b",)


@pytest.mark.parametrize(
    ("trials", "passes"),
    [([1, 2], [0]), ([0], [0]), ([2], [3]), ([2], [-1])],
)
def test_scenario_counts_reject_inconsistent_arrays(trials: list[int], passes: list[int]) -> None:
    with pytest.raises(ValueError, match="scenario"):
        ScenarioCounts.from_arrays(trials, passes)
