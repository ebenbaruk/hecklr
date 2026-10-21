"""Adaptive trials: group-sequential boundaries and where to spend extra trials (spec, "Adaptive").

Every extra look at the data is another chance of a false alarm. The Lan-DeMets spending function
that mimics O'Brien-Fleming fixes how much alpha may be spent by each information fraction t
(share of the planned trials done). As in ldbounds, gsDesign and rpact, each side spends alpha/2:

    alpha_side(t) = 2 - 2 * Phi(z_{1 - alpha/4} / sqrt(t)),   total = 2 * alpha_side(t)

Early looks get almost nothing, the final look nearly the full alpha. The boundary z_j at each look
is the value at which the probability of first crossing at look j, under no effect, equals the
alpha newly spent. That needs the joint law of the test statistics across looks, computed here by
the standard recursive numerical integration of the Brownian-motion score process (Armitage,
McPherson and Rowe, 1969) on a fixed grid, so the result is deterministic.
"""

import heapq
import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq
from scipy.stats import norm

from hecklr_stats.outcomes import FloatArray, ScenarioCounts

_GRID_POINTS = 2001
"""Odd, for Simpson's rule; boundaries agree with published tables to about 1e-3."""

MIN_LOOK_GAP = 0.01
"""Looks closer than this in information are refused: they add nothing, and the grid integration
loses accuracy when one step's spread is narrower than the grid spacing."""

_Z_MAX = 40.0
"""Cap for a look that spends (numerically) no alpha: it can never reject."""

FLIP_WEIGHT = 2.0
"""Flipped scenarios count double when allocating adaptive trials: a pass-to-fail flip is direct
evidence that the scenario drives the change, so pinning it down is worth more than its width."""


@dataclass(frozen=True, slots=True)
class Boundary:
    """The rejection rule at one look of a group-sequential design.

    Attributes:
        look: 1-based look number.
        information_fraction: Share of the planned trials completed at this look.
        alpha_spent: Cumulative two-sided alpha spent through this look.
        z: Two-sided critical value: reject when ``|Z| >= z``.
        nominal_alpha: The same rule as a p-value threshold, ``2 * (1 - Phi(z))``.
    """

    look: int
    information_fraction: float
    alpha_spent: float
    z: float
    nominal_alpha: float


def obrien_fleming_spending(information_fraction: float, alpha: float) -> float:
    """Cumulative two-sided alpha the Lan-DeMets O'Brien-Fleming function allows by fraction t."""
    if not 0.0 < information_fraction <= 1.0:
        raise ValueError(f"information fraction must be in (0, 1], got {information_fraction}")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    critical = norm.isf(alpha / 4)
    return float(4 * norm.sf(critical / math.sqrt(information_fraction)))


def group_sequential_boundaries(fractions: Sequence[float], alpha: float) -> list[Boundary]:
    """Two-sided O'Brien-Fleming-type boundaries for looks at the given information fractions.

    Args:
        fractions: Increasing information fractions in (0, 1], at least ``MIN_LOOK_GAP`` apart,
            one per look so far. Boundaries of earlier looks do not depend on later ones, so this
            can be called again with one more fraction at each new look.
        alpha: Overall two-sided significance level.
    """
    if not fractions or any(b - a < MIN_LOOK_GAP for a, b in itertools.pairwise(fractions)):
        raise ValueError(f"fractions must be non-empty and increase by >= {MIN_LOOK_GAP} per look")
    boundaries: list[Boundary] = []
    grid: FloatArray | None = None
    density: FloatArray | None = None
    previous_t, spent_before = 0.0, 0.0
    for look, t in enumerate(fractions, start=1):
        spent = obrien_fleming_spending(t, alpha)
        step_sd = math.sqrt(t - previous_t)
        if grid is None or density is None:
            z = min(float(norm.isf((spent - spent_before) / 2)), _Z_MAX)
        else:
            z = _solve_boundary(grid, density, t, step_sd, spent - spent_before)
        new_grid = np.linspace(-z * math.sqrt(t), z * math.sqrt(t), _GRID_POINTS)
        if grid is None or density is None:
            density = norm.pdf(new_grid, scale=math.sqrt(t))
        else:
            kernel = norm.pdf(new_grid[None, :], loc=grid[:, None], scale=step_sd)
            density = (_simpson_weights(grid) * density) @ kernel
        grid = new_grid
        boundaries.append(Boundary(look, t, spent, z, float(2 * norm.sf(z))))
        previous_t, spent_before = t, spent
    return boundaries


def _solve_boundary(
    grid: FloatArray, density: FloatArray, t: float, step_sd: float, increment: float
) -> float:
    """The z at which the chance of first crossing at this look equals ``increment``."""

    def excess(z: float) -> float:
        return _crossing_probability(grid, density, z * math.sqrt(t), step_sd) - increment

    if excess(_Z_MAX) >= 0:
        return _Z_MAX
    return float(brentq(excess, 0.0, _Z_MAX, xtol=1e-10))


def _crossing_probability(
    grid: FloatArray, density: FloatArray, bound: float, step_sd: float
) -> float:
    """P(not crossed before, and |B(t)| >= bound now) from the continuation density on ``grid``."""
    outside = norm.cdf((-bound - grid) / step_sd) + norm.cdf((grid - bound) / step_sd)
    return float(np.sum(_simpson_weights(grid) * density * outside))


def _simpson_weights(grid: FloatArray) -> FloatArray:
    step = float(grid[1] - grid[0])
    weights = np.ones_like(grid)
    weights[1:-1:2] = 4.0
    weights[2:-1:2] = 2.0
    return weights * step / 3.0


def information_fraction(trials_done: int, trials_planned: int) -> float:
    """Share of the planned trials completed, capped at 1 (a final look spends all alpha)."""
    if trials_planned < 1 or trials_done < 1:
        raise ValueError("trials_done and trials_planned must be >= 1")
    return min(1.0, trials_done / trials_planned)


def allocate_trials(
    baseline: ScenarioCounts, candidate: ScenarioCounts, budget: int
) -> dict[str, int]:
    """Spread ``budget`` extra candidate trials over the scenarios where they help most.

    Each trial goes, greedily, to the scenario whose extra trial shrinks the variance of the
    suite delta the most: ``v/n - v/(n+1)`` with ``v = p(1-p)`` at the smoothed rate
    ``(c+1)/(n+2)``, which favours wide intervals and still gives always-passing scenarios a little
    weight. Scenarios that flipped vs baseline (one side passes at least half its trials, the other
    does not) are weighted by ``FLIP_WEIGHT``. Only scenarios present in both runs are considered.

    Returns:
        Extra trials per scenario id, for scenarios that get at least one.
    """
    if budget < 0:
        raise ValueError(f"budget must be >= 0, got {budget}")
    base_rates = dict(zip(baseline.scenario_ids, baseline.rates, strict=True))
    paired = candidate.select(sid for sid in candidate.scenario_ids if sid in base_rates)
    heap: list[tuple[float, str, int, float]] = []
    for sid, n, c in zip(paired.scenario_ids, paired.trials, paired.passes, strict=True):
        smoothed = (c + 1) / (n + 2)
        flipped = (base_rates[sid] >= 0.5) != (c / n >= 0.5)
        weight = smoothed * (1 - smoothed) * (FLIP_WEIGHT if flipped else 1.0)
        heapq.heappush(heap, (-_gain(weight, int(n)), sid, int(n), weight))
    extra: dict[str, int] = {}
    for _ in range(budget if heap else 0):
        _, sid, n, weight = heapq.heappop(heap)
        extra[sid] = extra.get(sid, 0) + 1
        heapq.heappush(heap, (-_gain(weight, n + 1), sid, n + 1, weight))
    return dict(sorted(extra.items()))


def _gain(weight: float, trials: int) -> float:
    return weight / trials - weight / (trials + 1)
