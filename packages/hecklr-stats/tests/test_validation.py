"""SC5 (false alarms) and SC6 (power) on synthetic suites, plus interval calibration.

The reference suite size (60 scenarios x 5 trials per run) is the minimum found in
docs/adr/0005a-reference-suite-size-and-power.md; the reference agent suite (WS8) is sized from it.
"""

import numpy as np
import pytest

from hecklr_schemas import StatsConfig, Verdict
from hecklr_stats.outcomes import ScenarioCounts
from hecklr_stats.rates import suite_pass_at_k, suite_pass_hat_k, suite_pass_rate
from hecklr_stats.validation import (
    REALISTIC_SUITE,
    BetaComponent,
    SuiteModel,
    shift_pass_rates,
    verdict_rate,
)

REFERENCE_SCENARIOS = 60
REFERENCE_TRIALS = 5
# The regression verdict depends on the permutation p-value and the point delta, not on the
# bootstrap interval, so fewer resamples keep these 200-replicate simulations fast without
# changing what is measured. The permutation test stays valid at any resample count.
CONFIG = StatsConfig(bootstrap_resamples=2_000)


def test_sc5_aa_regression_false_alarm_rate_at_most_5_percent() -> None:
    rate = verdict_rate(
        n_scenarios=REFERENCE_SCENARIOS,
        trials=REFERENCE_TRIALS,
        drop=0.0,
        replicates=200,
        config=CONFIG,
        seed=5,
    )
    assert rate.value <= 0.05


def test_sc5_aa_improvement_false_alarm_rate_at_most_5_percent() -> None:
    rate = verdict_rate(
        n_scenarios=REFERENCE_SCENARIOS,
        trials=REFERENCE_TRIALS,
        drop=0.0,
        replicates=200,
        config=CONFIG,
        seed=6,
        verdict=Verdict.IMPROVEMENT,
    )
    assert rate.value <= 0.05


def test_sc5_aa_false_alarms_on_a_small_noisy_suite() -> None:
    # Few scenarios and 3 trials each: the exact permutation test must still hold its level.
    rate = verdict_rate(n_scenarios=12, trials=3, drop=0.0, replicates=200, config=CONFIG, seed=7)
    assert rate.value <= 0.05


def test_sc6_power_at_least_80_percent_for_a_10_point_drop() -> None:
    power = verdict_rate(
        n_scenarios=REFERENCE_SCENARIOS,
        trials=REFERENCE_TRIALS,
        drop=0.10,
        replicates=200,
        config=CONFIG,
        seed=8,
    )
    assert power.value >= 0.80


def test_sc6_power_for_a_drop_concentrated_on_30_percent_of_scenarios() -> None:
    # A fault that breaks a third of the suite; ADR 0005a measures 0.80 (2,000 replicates). This
    # guards against the procedure losing power on concentrated regressions.
    power = verdict_rate(
        n_scenarios=REFERENCE_SCENARIOS,
        trials=REFERENCE_TRIALS,
        drop=0.10,
        replicates=200,
        config=CONFIG,
        seed=9,
        affected_fraction=0.3,
    )
    assert power.ci_high >= 0.80


def test_too_small_a_suite_is_underpowered() -> None:
    # The sizing is not slack: 20 scenarios x 3 trials misses most 10-point drops.
    power = verdict_rate(n_scenarios=20, trials=3, drop=0.10, replicates=100, config=CONFIG, seed=1)
    assert power.value < 0.5


def test_suite_pass_rate_interval_covers_the_population_mean() -> None:
    # Cluster bootstrap targets the mean over the population of scenarios; ~95% nominal coverage.
    rng = np.random.default_rng(11)
    target = REALISTIC_SUITE.mean_pass_rate
    hits = 0
    for replicate in range(300):
        rates = REALISTIC_SUITE.sample_pass_rates(40, rng)
        counts = ScenarioCounts.from_arrays(np.full(40, 5), rng.binomial(5, rates))
        estimate = suite_pass_rate(counts, seed=replicate, resamples=1_000)
        hits += estimate.ci_low <= target <= estimate.ci_high
    assert 0.92 <= hits / 300 <= 0.99


@pytest.mark.parametrize(("metric", "power"), [("pass^3", 3), ("pass@3", 3)])
def test_suite_pass_k_interval_covers_the_population_value(metric: str, power: int) -> None:
    # pass^k is nonlinear in p, so the two-stage bootstrap needs recentring to cover ~95%.
    population = REALISTIC_SUITE.sample_pass_rates(1_000_000, np.random.default_rng(0))
    target = float(
        np.mean(population**power if metric.startswith("pass^") else 1 - (1 - population) ** power)
    )
    estimator = suite_pass_hat_k if metric.startswith("pass^") else suite_pass_at_k
    rng = np.random.default_rng(12)
    hits = 0
    for replicate in range(300):
        rates = REALISTIC_SUITE.sample_pass_rates(60, rng)
        counts = ScenarioCounts.from_arrays(np.full(60, 5), rng.binomial(5, rates))
        estimate = estimator(counts, power, seed=replicate, resamples=1_000)
        hits += estimate.ci_low <= target <= estimate.ci_high
    assert 0.92 <= hits / 300 <= 0.995


def test_realistic_suite_is_heterogeneous() -> None:
    rates = REALISTIC_SUITE.sample_pass_rates(10_000, np.random.default_rng(0))
    assert rates.mean() == pytest.approx(REALISTIC_SUITE.mean_pass_rate, abs=0.01)
    assert REALISTIC_SUITE.mean_pass_rate == pytest.approx(0.8, abs=0.01)
    assert np.mean(rates > 0.9) > 0.4
    assert np.mean((rates > 0.2) & (rates < 0.8)) > 0.2


def test_shift_pass_rates_hits_the_requested_drop() -> None:
    rates = REALISTIC_SUITE.sample_pass_rates(50, np.random.default_rng(1))
    everyone = np.ones(50, dtype=bool)
    shifted = shift_pass_rates(rates, 0.10, everyone)
    assert rates.mean() - shifted.mean() == pytest.approx(0.10, abs=1e-6)
    assert np.all(shifted <= rates + 1e-12)
    some = np.zeros(50, dtype=bool)
    some[:15] = True
    partial = shift_pass_rates(rates, 0.10, some)
    assert np.allclose(partial[15:], np.clip(rates[15:], 1e-6, 1 - 1e-6))
    assert shift_pass_rates(rates, 0.0, everyone) == pytest.approx(rates, abs=1e-6)
    with pytest.raises(ValueError, match="not reachable"):
        shift_pass_rates(np.full(10, 0.05), 0.10, everyone[:10])


def test_custom_suite_model() -> None:
    model = SuiteModel(components=(BetaComponent(weight=1.0, a=1.0, b=1.0),))
    assert model.mean_pass_rate == 0.5
    assert model.sample_pass_rates(5, np.random.default_rng(0)).shape == (5,)
