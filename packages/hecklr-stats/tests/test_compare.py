import time
from itertools import product

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from hecklr_schemas import Estimate, IntervalMethod, RunId, StatsConfig, Verdict, new_id
from hecklr_stats.compare import (
    MAX_TRIALS_NEEDED,
    PairedResult,
    benjamini_hochberg,
    compare_runs,
    decide_verdict,
    pair,
    paired_comparison,
    sign_flip_p_value,
    trials_needed,
)
from hecklr_stats.outcomes import ScenarioCounts, TrialOutcome

BASE = RunId(new_id())
CAND = RunId(new_id())
FAST = StatsConfig(bootstrap_resamples=2_000)


def _outcomes(
    rates: list[float], trials: int, *, labels: dict[str, str] | None = None
) -> list[TrialOutcome]:
    """Deterministic outcomes: scenario s passes its first round(rate * trials) trials."""
    return [
        TrialOutcome(scenario_id=f"s{s:02d}", passed=i < round(rate * trials), labels=labels or {})
        for s, rate in enumerate(rates)
        for i in range(trials)
    ]


def _delta(value: float, low: float, high: float) -> Estimate:
    return Estimate(
        value=value,
        ci_low=low,
        ci_high=high,
        n=10,
        method=IntervalMethod.PAIRED_CLUSTER_BOOTSTRAP,
    )


# --- sign-flip permutation test ------------------------------------------------------------


def test_exact_sign_flip_matches_hand_enumeration() -> None:
    # d = (1, 1, 1): only the all-plus and all-minus patterns reach |sum| = 3 -> p = 2/8.
    rng = np.random.default_rng(0)
    assert sign_flip_p_value([1.0, 1.0, 1.0], rng=rng, resamples=10) == pytest.approx(0.25)
    # d = (1, 2): |sum| >= 3 for 2 of 4 patterns.
    assert sign_flip_p_value([1.0, 2.0], rng=rng, resamples=10) == pytest.approx(0.5)


def test_sign_flip_of_no_differences_is_one() -> None:
    rng = np.random.default_rng(0)
    assert sign_flip_p_value(np.zeros(5), rng=rng, resamples=10) == 1.0
    assert sign_flip_p_value(np.zeros(40), rng=rng, resamples=100) == 1.0


def test_monte_carlo_sign_flip_agrees_with_exact() -> None:
    differences = np.random.default_rng(1).normal(0.3, 1.0, size=16)
    exact = sign_flip_p_value(differences, rng=np.random.default_rng(2), resamples=0)
    # Seventeen values force Monte Carlo; a zero added changes no statistic.
    padded = np.append(differences, 0.0)
    approx = sign_flip_p_value(padded, rng=np.random.default_rng(2), resamples=20_000)
    assert approx == pytest.approx(exact, abs=0.01)


def test_monte_carlo_p_value_is_never_zero_and_is_seeded() -> None:
    differences = np.full(30, 0.5)
    first = sign_flip_p_value(differences, rng=np.random.default_rng(3), resamples=999)
    again = sign_flip_p_value(differences, rng=np.random.default_rng(3), resamples=999)
    assert first == again == pytest.approx(1 / 1000)


def test_sign_flip_is_uniform_under_the_null() -> None:
    # Symmetric differences: P(p <= alpha) must not exceed alpha.
    rng = np.random.default_rng(4)
    p_values = [sign_flip_p_value(rng.normal(size=12), rng=rng, resamples=0) for _ in range(1_000)]
    assert np.mean(np.array(p_values) <= 0.05) <= 0.05 + 0.015


# --- Benjamini-Hochberg ------------------------------------------------------------------


def test_benjamini_hochberg_matches_reference() -> None:
    # Reference: R p.adjust(c(0.01, 0.04, 0.03, 0.005, 0.5), method = "BH").
    q = benjamini_hochberg([0.01, 0.04, 0.03, 0.005, 0.5])
    assert q.tolist() == pytest.approx([0.025, 0.05, 0.05, 0.025, 0.5])


@given(st.lists(st.floats(0.0, 1.0), min_size=1, max_size=40))
def test_benjamini_hochberg_is_monotone_and_bounded(p_values: list[float]) -> None:
    p = np.array(p_values)
    q = benjamini_hochberg(p)
    assert np.all(q >= p - 1e-15)
    assert np.all(q <= 1.0)
    order = np.argsort(p, kind="stable")
    assert np.all(np.diff(q[order]) >= -1e-15)


def test_benjamini_hochberg_edge_cases() -> None:
    assert benjamini_hochberg([]).size == 0
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        benjamini_hochberg([1.5])


# --- verdict rule ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "low", "high", "p", "expected"),
    [
        (-0.10, -0.15, -0.05, 0.01, Verdict.REGRESSION),
        (0.10, 0.05, 0.15, 0.01, Verdict.IMPROVEMENT),
        (-0.02, -0.08, 0.04, 0.01, Verdict.INCONCLUSIVE),  # significant but below delta
        (0.01, -0.02, 0.025, 0.40, Verdict.NO_CHANGE),
        (-0.10, -0.20, 0.00, 0.06, Verdict.INCONCLUSIVE),
        (0.00, -0.03, 0.03, 1.00, Verdict.NO_CHANGE),  # bounds inclusive
    ],
)
def test_us04_verdict_rule_follows_the_spec_table(
    value: float, low: float, high: float, p: float, expected: Verdict
) -> None:
    assert decide_verdict(_delta(value, low, high), p, alpha=0.05, min_effect=0.03) is expected


@given(
    value=st.floats(-1, 1),
    spread=st.tuples(st.floats(0, 1), st.floats(0, 1)),
    p=st.floats(0, 1),
    alpha=st.floats(0.001, 0.2),
    min_effect=st.floats(0, 0.2),
)
def test_verdict_rule_is_consistent(
    value: float, spread: tuple[float, float], p: float, alpha: float, min_effect: float
) -> None:
    delta = _delta(value, value - spread[0], value + spread[1])
    verdict = decide_verdict(delta, p, alpha, min_effect)
    if verdict is Verdict.REGRESSION:
        assert p < alpha
        assert value < -min_effect
    elif verdict is Verdict.IMPROVEMENT:
        assert p < alpha
        assert value > min_effect
    elif verdict is Verdict.NO_CHANGE:
        assert -min_effect <= delta.ci_low
        assert delta.ci_high <= min_effect
    # Never both a significant change and a no-change claim.
    if p < alpha and abs(value) > min_effect:
        assert verdict in (Verdict.REGRESSION, Verdict.IMPROVEMENT)
    # Mirror symmetry: negating the effect swaps regression and improvement.
    mirrored = decide_verdict(_delta(-value, -delta.ci_high, -delta.ci_low), p, alpha, min_effect)
    swap = {Verdict.REGRESSION: Verdict.IMPROVEMENT, Verdict.IMPROVEMENT: Verdict.REGRESSION}
    assert mirrored is swap.get(verdict, verdict)


# --- paired comparison --------------------------------------------------------------------


def test_paired_comparison_estimates_mean_of_differences() -> None:
    baseline = ScenarioCounts.from_arrays([10, 10, 4], [10, 5, 4])
    candidate = ScenarioCounts.from_arrays([10, 10, 4], [8, 5, 2])
    result = paired_comparison(
        baseline, candidate, rng=np.random.default_rng(0), resamples=2_000, confidence=0.95
    )
    assert result.delta.value == pytest.approx((-0.2 + 0.0 - 0.5) / 3)
    assert result.baseline.value == pytest.approx((1.0 + 0.5 + 1.0) / 3)
    assert result.delta.method is IntervalMethod.PAIRED_CLUSTER_BOOTSTRAP
    assert result.baseline.method is IntervalMethod.CLUSTER_BOOTSTRAP
    assert result.n_scenarios == 3
    assert (result.n_trials_baseline, result.n_trials_candidate) == (24, 24)
    assert result.delta.ci_low <= result.delta.value <= result.delta.ci_high
    assert result.p_value == pytest.approx(0.5)  # two non-zero differences, same sign: 2/4


def test_paired_comparison_requires_paired_counts() -> None:
    a = ScenarioCounts.from_outcomes(_outcomes([1.0, 0.5], 2))
    b = ScenarioCounts.from_outcomes(_outcomes([1.0], 2))
    rng = np.random.default_rng(0)
    with pytest.raises(ValueError, match="pair"):
        paired_comparison(a, b, rng=rng, resamples=10, confidence=0.95)
    empty = a.select([])
    with pytest.raises(ValueError, match="no scenarios"):
        paired_comparison(empty, empty, rng=rng, resamples=10, confidence=0.95)
    paired_a, paired_b = pair(a, b)
    assert paired_a.scenario_ids == paired_b.scenario_ids == ("s00",)


def test_paired_bootstrap_ci_covers_the_true_delta() -> None:
    # 200 suites with a known true delta; the 95% interval should cover it about 95% of the time.
    rng = np.random.default_rng(10)
    hits = 0
    for _ in range(200):
        base_rates = rng.beta(4, 1.5, size=40)
        cand_rates = np.clip(base_rates - 0.05, 0, 1)
        true_delta = float(np.mean(cand_rates - base_rates))
        baseline = ScenarioCounts.from_arrays(np.full(40, 8), rng.binomial(8, base_rates))
        candidate = ScenarioCounts.from_arrays(np.full(40, 8), rng.binomial(8, cand_rates))
        result = paired_comparison(baseline, candidate, rng=rng, resamples=1_000, confidence=0.95)
        hits += result.delta.ci_low <= true_delta <= result.delta.ci_high
    assert 0.92 <= hits / 200 <= 1.0


# --- trials needed --------------------------------------------------------------------------


def _result(delta: float, se: float, scenarios: int = 40, trials: int = 3) -> PairedResult:
    estimate = Estimate(
        value=0.8, ci_low=0.7, ci_high=0.9, n=scenarios, method=IntervalMethod.CLUSTER_BOOTSTRAP
    )
    return PairedResult(
        baseline=estimate,
        candidate=estimate,
        delta=Estimate(
            value=delta,
            ci_low=delta - 2 * se,
            ci_high=delta + 2 * se,
            n=scenarios,
            method=IntervalMethod.PAIRED_CLUSTER_BOOTSTRAP,
        ),
        p_value=0.2,
        delta_se=se,
        n_trials_baseline=scenarios * trials,
        n_trials_candidate=scenarios * trials,
    )


def test_trials_needed_scales_with_the_variance_gap() -> None:
    # |delta| = 0.06 > delta_min: target se = 0.06 / (1.96 + 0.84) = 0.0214; se 0.04 -> x3.49.
    assert trials_needed(_result(-0.06, 0.04), alpha=0.05, min_effect=0.03) == 8
    # Noisier estimates need more trials.
    assert trials_needed(_result(-0.06, 0.06), alpha=0.05, min_effect=0.03) == 21
    # Aiming for no-change: margin = 0.03 - 0.01 = 0.02, target se = 0.02 / 2.80 = 0.00714.
    assert trials_needed(_result(0.01, 0.01), alpha=0.05, min_effect=0.03) == 3
    # Just beyond delta_min, the estimate must also land beyond it: target se = 0.01 / 0.84.
    assert trials_needed(_result(-0.04, 0.03), alpha=0.05, min_effect=0.03) == 17


def test_trials_needed_is_none_when_trials_cannot_decide() -> None:
    assert trials_needed(_result(-0.2, 0.1, scenarios=5), alpha=0.05, min_effect=0.03) is None
    assert trials_needed(_result(-0.03, 0.05), alpha=0.05, min_effect=0.03) is None
    assert trials_needed(_result(-0.06, 0.0), alpha=0.05, min_effect=0.03) is None
    # A delta a hair beyond delta_min needs more than MAX_TRIALS_NEEDED trials to settle.
    assert trials_needed(_result(-0.0301, 0.05), alpha=0.05, min_effect=0.03) is None
    needed = trials_needed(_result(-0.031, 0.005), alpha=0.05, min_effect=0.03)
    assert needed is not None
    assert needed <= MAX_TRIALS_NEEDED


def test_trials_needed_is_at_least_one() -> None:
    assert trials_needed(_result(-0.2, 0.01), alpha=0.05, min_effect=0.03) == 1


# --- compare_runs ------------------------------------------------------------------------------


def test_us04_compare_runs_flags_a_clear_regression() -> None:
    baseline = _outcomes([0.9] * 30, 10)
    candidate = _outcomes([0.6] * 30, 10)
    (suite,) = compare_runs(
        baseline, candidate, baseline_run=BASE, candidate_run=CAND, config=FAST, seed=0
    )
    assert suite.verdict is Verdict.REGRESSION
    assert suite.delta.value == pytest.approx(-0.3)
    assert suite.p_value < 0.001
    assert suite.q_value == suite.p_value
    assert suite.slice == {}
    assert (suite.n_scenarios, suite.n_trials_baseline, suite.n_trials_candidate) == (30, 300, 300)
    assert (suite.look, suite.alpha_spent, suite.trials_needed) == (1, None, None)
    assert (suite.alpha, suite.min_effect) == (0.05, 0.03)
    assert suite.delta.confidence == pytest.approx(0.95)


def test_compare_runs_on_identical_runs_is_no_change() -> None:
    outcomes = _outcomes([1.0, 0.0, 1.0, 1.0] * 10, 5)
    (suite,) = compare_runs(
        outcomes, outcomes, baseline_run=BASE, candidate_run=CAND, config=FAST, seed=0
    )
    assert suite.verdict is Verdict.NO_CHANGE
    assert (suite.delta.value, suite.p_value) == (0.0, 1.0)


def test_compare_runs_reports_trials_needed_when_inconclusive() -> None:
    rng = np.random.default_rng(5)
    rates = rng.beta(3, 1.5, size=30)

    def run() -> list[TrialOutcome]:
        return [
            TrialOutcome(scenario_id=f"s{s}", passed=bool(rng.random() < rate))
            for s, rate in enumerate(rates)
            for _ in range(3)
        ]

    (suite,) = compare_runs(
        run(), run(), baseline_run=BASE, candidate_run=CAND, config=FAST, seed=0
    )
    assert suite.verdict is Verdict.INCONCLUSIVE
    assert suite.trials_needed is not None
    assert suite.trials_needed >= 1


def test_compare_runs_slices_with_benjamini_hochberg() -> None:
    def run(voice_rate: float) -> list[TrialOutcome]:
        chat = _outcomes([1.0] * 20, 10, labels={"channel": "chat"})
        voice = [
            TrialOutcome(
                scenario_id=f"v{s}",
                passed=i < round(voice_rate * 10),
                labels={"channel": "voice"},
                tags=frozenset({"noisy"}),
            )
            for s in range(20)
            for i in range(10)
        ]
        return chat + voice

    comparisons = compare_runs(
        run(0.9), run(0.5), baseline_run=BASE, candidate_run=CAND, config=FAST, seed=0
    )
    by_slice = {tuple(c.slice.items()): c for c in comparisons}
    assert list(by_slice) == [
        (),
        (("channel", "chat"),),
        (("channel", "voice"),),
        (("tag", "noisy"),),
    ]
    assert by_slice[(("channel", "voice"),)].verdict is Verdict.REGRESSION
    assert by_slice[(("tag", "noisy"),)].verdict is Verdict.REGRESSION
    assert by_slice[(("channel", "chat"),)].verdict is Verdict.NO_CHANGE
    assert by_slice[()].delta.value == pytest.approx(-0.2)
    slice_p = [c.p_value for c in comparisons[1:]]
    slice_q = [c.q_value for c in comparisons[1:]]
    assert slice_q == pytest.approx(benjamini_hochberg(slice_p).tolist())
    assert all(q >= p for p, q in zip(slice_p, slice_q, strict=True))


def test_slice_q_values_decide_slice_verdicts() -> None:
    # Ten tags of six scenarios; only tag t0 drops (by 50 points). Its raw p = 2/2^6 < alpha, but
    # across ten slices its q-value is 10x larger, so it is not called a regression.
    baseline: list[TrialOutcome] = []
    candidate: list[TrialOutcome] = []
    for tag in range(10):
        tags = frozenset({f"t{tag}"})
        for s in range(6):
            for i in range(4):
                sid = f"t{tag}s{s}"
                baseline.append(TrialOutcome(scenario_id=sid, passed=tag == 0 or i < 2, tags=tags))
                candidate.append(TrialOutcome(scenario_id=sid, passed=i < 2, tags=tags))
    comparisons = compare_runs(
        baseline, candidate, baseline_run=BASE, candidate_run=CAND, config=FAST, seed=0
    )
    flagged = next(c for c in comparisons if c.slice == {"tag": "t0"})
    assert flagged.p_value == pytest.approx(2 / 64)
    assert flagged.q_value == pytest.approx(10 * 2 / 64)
    assert flagged.verdict is Verdict.INCONCLUSIVE


def test_compare_runs_is_reproducible_and_honours_config_seed() -> None:
    rng = np.random.default_rng(6)
    base = [
        TrialOutcome(scenario_id=f"s{i % 25}", passed=bool(rng.random() < 0.7)) for i in range(100)
    ]
    cand = [
        TrialOutcome(scenario_id=f"s{i % 25}", passed=bool(rng.random() < 0.7)) for i in range(100)
    ]

    def deltas(config: StatsConfig, seed: int) -> Estimate:
        (suite,) = compare_runs(
            base, cand, baseline_run=BASE, candidate_run=CAND, config=config, seed=seed
        )
        return suite.delta

    assert deltas(FAST, 1) == deltas(FAST, 1)
    assert deltas(FAST, 1) != deltas(FAST, 2)
    pinned = StatsConfig(bootstrap_resamples=2_000, seed=9)
    assert deltas(pinned, 1) == deltas(pinned, 2)


def test_compare_runs_pairs_only_shared_scenarios() -> None:
    baseline = _outcomes([1.0, 1.0, 1.0], 4)
    candidate = [*_outcomes([1.0, 1.0], 4), TrialOutcome(scenario_id="new", passed=False)]
    (suite,) = compare_runs(
        baseline, candidate, baseline_run=BASE, candidate_run=CAND, config=FAST, seed=0
    )
    assert suite.n_scenarios == 2
    assert suite.n_trials_candidate == 8
    with pytest.raises(ValueError, match="no scenarios in common"):
        compare_runs(
            baseline,
            [TrialOutcome(scenario_id="other", passed=True)],
            baseline_run=BASE,
            candidate_run=CAND,
            config=FAST,
            seed=0,
        )


def test_compare_runs_skips_slices_without_paired_scenarios() -> None:
    baseline = _outcomes([1.0] * 3, 2, labels={"channel": "chat"})
    candidate = _outcomes([1.0] * 3, 2, labels={"channel": "voice"})
    comparisons = compare_runs(
        baseline, candidate, baseline_run=BASE, candidate_run=CAND, config=FAST, seed=0
    )
    assert [c.slice for c in comparisons] == [{}]


def test_compare_runs_records_the_group_sequential_look() -> None:
    baseline = _outcomes([0.9] * 30, 10)
    candidate = _outcomes([0.8] * 30, 10)
    fixed, *_ = compare_runs(
        baseline, candidate, baseline_run=BASE, candidate_run=CAND, config=FAST, seed=0
    )
    first, *_ = compare_runs(
        baseline,
        candidate,
        baseline_run=BASE,
        candidate_run=CAND,
        config=FAST,
        seed=0,
        look_fractions=[0.25],
    )
    assert fixed.verdict is Verdict.REGRESSION
    # p ~ 2/2^30 passes alpha but O'Brien-Fleming spends ~1e-5 at a quarter of the information.
    assert first.look == 1
    assert first.alpha_spent is not None
    assert first.alpha_spent < 1e-4
    second, *_ = compare_runs(
        baseline,
        candidate,
        baseline_run=BASE,
        candidate_run=CAND,
        config=FAST,
        seed=0,
        look_fractions=[0.25, 1.0],
    )
    assert second.look == 2
    assert second.alpha_spent == pytest.approx(0.05)
    assert second.verdict is Verdict.REGRESSION


def test_compare_runs_with_alpha_spending_withholds_marginal_early_verdicts() -> None:
    # Delta -0.1 on 12 scenarios: exact p = 2/4096, below 0.05 but above the first OBF threshold.
    baseline = _outcomes([0.9] * 12, 10)
    candidate = _outcomes([0.8] * 12, 10)
    early, *_ = compare_runs(
        baseline,
        candidate,
        baseline_run=BASE,
        candidate_run=CAND,
        config=FAST,
        seed=0,
        look_fractions=[0.2],
    )
    assert early.p_value == pytest.approx(2 / 4096)
    assert early.verdict is Verdict.INCONCLUSIVE


def test_full_comparison_on_200_by_10_with_10k_resamples_is_fast() -> None:
    rng = np.random.default_rng(7)
    rates = rng.beta(4, 1, size=200)
    cells = list(product(range(200), range(10)))
    base = [
        TrialOutcome(scenario_id=f"s{s}", passed=bool(rng.random() < rates[s])) for s, _ in cells
    ]
    cand = [
        TrialOutcome(scenario_id=f"s{s}", passed=bool(rng.random() < rates[s])) for s, _ in cells
    ]
    start = time.perf_counter()
    compare_runs(base, cand, baseline_run=BASE, candidate_run=CAND, config=StatsConfig(), seed=0)
    assert time.perf_counter() - start < 2.0


def test_each_run_pass_rate_in_a_comparison_avoids_the_collapsed_interval() -> None:
    # Issue #14 / ADR 0005c also hold for the per-run rates a comparison reports.
    all_pass = ScenarioCounts.from_arrays([2] * 5, [2] * 5)
    mixed = ScenarioCounts.from_arrays([2] * 5, [2, 2, 2, 2, 1])
    result = paired_comparison(
        all_pass, mixed, rng=np.random.default_rng(0), resamples=1_000, confidence=0.95
    )
    assert result.baseline.method is IntervalMethod.WILSON
    assert result.baseline.ci_low == pytest.approx(0.5655, abs=1e-4)
    assert result.candidate.method is IntervalMethod.CLUSTER_BOOTSTRAP
    assert result.delta.method is IntervalMethod.PAIRED_CLUSTER_BOOTSTRAP
