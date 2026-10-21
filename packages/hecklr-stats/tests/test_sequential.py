import itertools

import numpy as np
import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from hecklr_stats.outcomes import ScenarioCounts
from hecklr_stats.sequential import (
    allocate_trials,
    group_sequential_boundaries,
    information_fraction,
    obrien_fleming_spending,
)


@pytest.mark.parametrize(
    ("looks", "expected"),
    [
        # Lan-DeMets O'Brien-Fleming, two-sided alpha = 0.05, equally spaced looks; reference
        # values from rpact::getDesignGroupSequential(typeOfDesign = "asOF", sided = 2) and the
        # ldbounds tables.
        (2, [2.963, 1.969]),
        (3, [3.710, 2.511, 1.993]),
        (5, [4.877, 3.357, 2.680, 2.290, 2.031]),
    ],
)
def test_boundaries_match_published_values(looks: int, expected: list[float]) -> None:
    fractions = [i / looks for i in range(1, looks + 1)]
    boundaries = group_sequential_boundaries(fractions, alpha=0.05)
    assert [b.z for b in boundaries] == pytest.approx(expected, abs=2e-3)
    assert [b.look for b in boundaries] == list(range(1, looks + 1))
    assert boundaries[-1].alpha_spent == pytest.approx(0.05)


def test_single_look_is_the_fixed_design() -> None:
    (only,) = group_sequential_boundaries([1.0], alpha=0.05)
    assert only.z == pytest.approx(1.959964, abs=1e-5)
    assert only.nominal_alpha == pytest.approx(0.05)


def test_repeated_looks_keep_the_overall_false_alarm_rate() -> None:
    # Under no effect, the z-statistic at look j is a Brownian motion B(t_j) / sqrt(t_j).
    fractions = np.array([0.3, 0.55, 0.8, 1.0])
    boundaries = group_sequential_boundaries(fractions.tolist(), alpha=0.05)
    rng = np.random.default_rng(0)
    steps = rng.normal(size=(400_000, fractions.size)) * np.sqrt(np.diff(fractions, prepend=0.0))
    z = np.cumsum(steps, axis=1) / np.sqrt(fractions)
    crossed = np.abs(z) >= np.array([b.z for b in boundaries])
    assert crossed.any(axis=1).mean() == pytest.approx(0.05, abs=0.002)
    # Each look spends what the function allows.
    first_cross = np.where(crossed.any(axis=1), crossed.argmax(axis=1), -1)
    spent = [np.mean((first_cross >= 0) & (first_cross <= j)) for j in range(fractions.size)]
    assert spent == pytest.approx([b.alpha_spent for b in boundaries], abs=0.002)


def test_peeking_without_spending_inflates_false_alarms() -> None:
    # The reason the design exists: testing each look at 0.05 rejects far more than 5%.
    fractions = np.array([0.25, 0.5, 0.75, 1.0])
    rng = np.random.default_rng(1)
    steps = rng.normal(size=(200_000, 4)) * np.sqrt(0.25)
    z = np.cumsum(steps, axis=1) / np.sqrt(fractions)
    assert (np.abs(z) >= 1.96).any(axis=1).mean() > 0.10


@settings(deadline=None, max_examples=40)
@given(st.lists(st.floats(0.01, 1.0), min_size=1, max_size=5))
def test_boundaries_are_monotone_in_information(gaps: list[float]) -> None:
    fractions = np.minimum(np.cumsum(gaps) / max(1.0, sum(gaps)), 1.0).tolist()
    assume(all(b - a >= 0.01 for a, b in itertools.pairwise(fractions)))
    boundaries = group_sequential_boundaries(fractions, alpha=0.05)
    spent = [b.alpha_spent for b in boundaries]
    assert spent == sorted(spent)
    assert all(0.0 <= b.nominal_alpha <= 0.05 + 1e-9 for b in boundaries)
    assert spent[-1] <= 0.05 + 1e-12


def test_a_look_that_spends_nothing_never_rejects() -> None:
    first, _ = group_sequential_boundaries([0.001, 1.0], alpha=0.05)
    assert first.z == pytest.approx(40.0)
    assert first.nominal_alpha == 0.0


def test_spending_function_shape() -> None:
    assert obrien_fleming_spending(1.0, 0.05) == pytest.approx(0.05)
    assert obrien_fleming_spending(0.5, 0.05) == pytest.approx(0.00305, abs=1e-5)
    with pytest.raises(ValueError, match="information fraction"):
        obrien_fleming_spending(0.0, 0.05)
    with pytest.raises(ValueError, match="alpha"):
        obrien_fleming_spending(0.5, 1.0)


@pytest.mark.parametrize("fractions", [[], [0.5, 0.5], [0.8, 0.4], [0.5, 0.505], [0.5, 1.2]])
def test_boundaries_reject_bad_fractions(fractions: list[float]) -> None:
    with pytest.raises(ValueError, match="fraction"):
        group_sequential_boundaries(fractions, alpha=0.05)


def test_information_fraction_is_capped() -> None:
    assert information_fraction(150, 300) == 0.5
    assert information_fraction(400, 300) == 1.0
    with pytest.raises(ValueError, match=">= 1"):
        information_fraction(0, 300)


def test_allocation_spends_the_budget_on_flipped_and_uncertain_scenarios() -> None:
    baseline = ScenarioCounts.from_arrays([3, 3, 3, 3], [3, 3, 3, 1])
    # s0000 and s0002 always pass, s0001 flipped to failing, s0003 noisy but not flipped.
    candidate = ScenarioCounts.from_arrays([3, 3, 3, 3], [3, 0, 3, 1])
    extra = allocate_trials(baseline, candidate, budget=12)
    assert sum(extra.values()) == 12
    assert extra["s0001"] == max(extra.values())
    assert extra["s0003"] > extra.get("s0000", 0)
    assert extra.get("s0000", 0) == extra.get("s0002", 0)


def test_allocation_is_deterministic_and_skips_unpaired_scenarios() -> None:
    baseline = ScenarioCounts.from_arrays([5, 5], [5, 5])
    candidate = ScenarioCounts(
        scenario_ids=("s0000", "s0001", "zz-new"),
        trials=np.array([5, 5, 5]),
        passes=np.array([5, 5, 0]),
    )
    extra = allocate_trials(baseline, candidate, budget=3)
    assert extra == allocate_trials(baseline, candidate, budget=3)
    assert "zz-new" not in extra
    assert extra == {"s0000": 2, "s0001": 1}


def test_allocation_with_no_budget_or_no_pairs() -> None:
    counts = ScenarioCounts.from_arrays([3], [1])
    assert allocate_trials(counts, counts, budget=0) == {}
    assert allocate_trials(counts, counts.select([]), budget=5) == {}
    with pytest.raises(ValueError, match="budget"):
        allocate_trials(counts, counts, budget=-1)
