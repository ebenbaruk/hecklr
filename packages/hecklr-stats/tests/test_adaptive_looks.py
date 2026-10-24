"""Issue #103: ``--adaptive`` decides at group-sequential looks with O'Brien-Fleming boundaries."""

import pytest

from hecklr_schemas import RunId, StatsConfig, Verdict, new_id
from hecklr_stats import compare_sequentially, group_sequential_boundaries
from hecklr_stats.outcomes import TrialOutcome
from hecklr_stats.validation import adaptive_verdict_rate

BASE = RunId(new_id())
CAND = RunId(new_id())
FAST = StatsConfig(bootstrap_resamples=1_000)
SCENARIOS = 20
TRIALS = 4


def _trials(failing: int, trials: int) -> list[TrialOutcome]:
    """``trials`` trials of every scenario; the first ``failing`` scenarios fail all of them."""
    return [
        TrialOutcome(scenario_id=f"s{s:02d}", passed=s >= failing)
        for s in range(SCENARIOS)
        for _ in range(trials)
    ]


def _looks(failing: int) -> list[list[TrialOutcome]]:
    """A first pass of ``TRIALS`` trials per scenario, then 2 more each: looks at 2/3 and 1."""
    first = _trials(failing, TRIALS)
    return [first, first + _trials(failing, 2)]


def test_issue103_a_first_look_that_crosses_its_boundary_decides() -> None:
    # 10 of 20 scenarios fail: exact sign-flip p = 2 / 2**10 ~ 0.002, below look 1's nominal alpha.
    looks = _looks(failing=10)
    fractions = [len(look) / len(looks[-1]) for look in looks]
    first, _ = group_sequential_boundaries(fractions, FAST.alpha)

    comparisons = compare_sequentially(
        _trials(0, TRIALS), looks, baseline_run=BASE, candidate_run=CAND, config=FAST, seed=1
    )

    suite = comparisons[0]
    assert suite.verdict is Verdict.REGRESSION
    assert suite.look == 1
    assert suite.alpha_spent == pytest.approx(first.alpha_spent)
    assert suite.n_trials_candidate == len(looks[0])
    assert all(c.look == 1 for c in comparisons)


def test_issue103_a_first_look_below_the_full_alpha_but_above_its_boundary_waits() -> None:
    # 7 failing scenarios: p = 2 / 2**7 ~ 0.016, under 0.05 but over look 1's ~0.012. A fixed
    # design would have called a regression at the first pass; the sequential one waits.
    looks = _looks(failing=7)

    comparisons = compare_sequentially(
        _trials(0, TRIALS), looks, baseline_run=BASE, candidate_run=CAND, config=FAST, seed=1
    )

    suite = comparisons[0]
    assert suite.verdict is Verdict.REGRESSION
    assert suite.look == 2
    assert suite.alpha_spent == pytest.approx(FAST.alpha)
    assert suite.n_trials_candidate == len(looks[1])


def test_issue103_a_single_look_is_the_fixed_design_with_its_alpha_recorded() -> None:
    (only,) = compare_sequentially(
        _trials(0, TRIALS),
        [_trials(0, TRIALS)],
        baseline_run=BASE,
        candidate_run=CAND,
        config=FAST,
        seed=1,
    )

    assert only.look == 1
    assert only.alpha_spent == pytest.approx(FAST.alpha)


def test_issue103_looks_must_grow_and_end_with_every_trial() -> None:
    first = _trials(0, TRIALS)
    with pytest.raises(ValueError, match="at least one look"):
        compare_sequentially(first, [], baseline_run=BASE, candidate_run=CAND, config=FAST, seed=1)
    with pytest.raises(ValueError, match="fractions"):
        compare_sequentially(
            first, [first, first], baseline_run=BASE, candidate_run=CAND, config=FAST, seed=1
        )


BOTH = (Verdict.REGRESSION, Verdict.IMPROVEMENT)


@pytest.mark.parametrize(("n_scenarios", "trials", "seed"), [(60, 5, 103), (20, 3, 104)])
def test_issue103_sc5_adaptive_false_alarm_rate_across_looks_at_most_alpha(
    n_scenarios: int, trials: int, seed: int
) -> None:
    # The whole adaptive procedure: first pass, extra trials allocated from what it saw, then a
    # look at each batch, with the gate's verdict rule. Regressions and improvements together:
    # every way an A/A comparison can raise an alarm.
    rate = adaptive_verdict_rate(
        n_scenarios=n_scenarios,
        trials=trials,
        drop=0.0,
        replicates=400,
        config=FAST,
        seed=seed,
        verdicts=BOTH,
    )

    assert rate.n == 400
    assert rate.value <= FAST.alpha


def test_issue103_the_test_alone_across_looks_is_consistent_with_alpha() -> None:
    # With min_effect 0 every rejection of the permutation test is a verdict. Over 2,000
    # replicates it rejects 5.4% [4.5%, 6.5%] across looks, against 5.6% with the final look
    # alone and 4.0% for a fixed design: the looks add nothing, the variance-targeted extra batch
    # about a point and a half, which the minimum effect absorbs (ADR 0024-hstats).
    rate = adaptive_verdict_rate(
        n_scenarios=60,
        trials=5,
        drop=0.0,
        replicates=400,
        config=StatsConfig(bootstrap_resamples=1_000, min_effect=0.0),
        seed=106,
        verdicts=BOTH,
    )

    assert rate.ci_low <= FAST.alpha


def test_issue103_adaptive_keeps_its_power_for_a_10_point_drop() -> None:
    power = adaptive_verdict_rate(
        n_scenarios=60, trials=5, drop=0.10, replicates=100, config=FAST, seed=105
    )

    assert power.value >= 0.80
