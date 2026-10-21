"""Synthetic Bernoulli suites for validating the statistics (spec, "Validating the statistics").

A/A false-alarm rate (SC5) and power (SC6) are properties of the whole decision procedure on
realistic suites, so they are measured by simulation rather than assumed. Real suites are
heterogeneous: most scenarios almost always pass, some usually do, a few are hard or flaky.
``REALISTIC_SUITE`` encodes that as a mixture of Beta distributions of per-scenario pass rates.
"""

from dataclasses import dataclass

import numpy as np
from scipy.optimize import brentq
from scipy.special import expit, logit

from hecklr_schemas import Estimate, StatsConfig, Verdict
from hecklr_stats.compare import decide_verdict, paired_comparison
from hecklr_stats.intervals import wilson_estimate
from hecklr_stats.outcomes import FloatArray, ScenarioCounts

_RATE_FLOOR = 1e-6
"""Pass rates are kept inside (0, 1) so a logit shift is defined for every scenario."""


@dataclass(frozen=True, slots=True)
class BetaComponent:
    """One kind of scenario: a share of the suite whose pass rates follow Beta(a, b)."""

    weight: float
    a: float
    b: float


@dataclass(frozen=True, slots=True)
class SuiteModel:
    """A distribution of per-scenario pass rates: a mixture of Beta components."""

    components: tuple[BetaComponent, ...]

    def sample_pass_rates(self, n_scenarios: int, rng: np.random.Generator) -> FloatArray:
        """Draw true pass rates for ``n_scenarios`` scenarios."""
        weights = np.array([c.weight for c in self.components])
        kinds = rng.choice(len(self.components), size=n_scenarios, p=weights / weights.sum())
        a = np.array([c.a for c in self.components])[kinds]
        b = np.array([c.b for c in self.components])[kinds]
        return np.asarray(rng.beta(a, b), dtype=np.float64)

    @property
    def mean_pass_rate(self) -> float:
        """Expected pass rate of a random scenario."""
        total = sum(c.weight for c in self.components)
        return sum(c.weight * c.a / (c.a + c.b) for c in self.components) / total


REALISTIC_SUITE = SuiteModel(
    components=(
        BetaComponent(weight=0.5, a=20.0, b=1.0),  # reliable: mean 0.95
        BetaComponent(weight=0.3, a=6.0, b=2.0),  # usually fine: mean 0.75
        BetaComponent(weight=0.2, a=2.0, b=2.0),  # hard or flaky: mean 0.50
    )
)
"""Reference heterogeneity: suite pass rate about 0.80, a fifth of scenarios near coin flips."""


def shift_pass_rates(rates: FloatArray, drop: float, affected: np.ndarray) -> FloatArray:
    """Lower the ``affected`` rates by one logit shift so the suite mean drops by ``drop``.

    A shared logit shift is how a real regression behaves: scenarios near 100% move little, those
    in the middle move most. ``drop`` is in absolute pass-rate points (0.10 = 10 points).
    """
    clipped = np.clip(rates, _RATE_FLOOR, 1 - _RATE_FLOOR)
    if drop == 0:
        return clipped
    n_scenarios = clipped.size
    reachable = float(clipped[affected].sum()) / n_scenarios
    if not 0 < drop < reachable:
        raise ValueError(f"a drop of {drop} is not reachable on the affected scenarios")

    def shifted(shift: float) -> FloatArray:
        out = clipped.copy()
        out[affected] = expit(logit(clipped[affected]) - shift)
        return out

    shift = brentq(lambda s: float(clipped.mean() - shifted(s).mean()) - drop, 0.0, 60.0)
    return shifted(float(shift))


def verdict_rate(
    *,
    n_scenarios: int,
    trials: int,
    drop: float,
    replicates: int,
    config: StatsConfig,
    seed: int,
    verdict: Verdict = Verdict.REGRESSION,
    affected_fraction: float = 1.0,
    model: SuiteModel = REALISTIC_SUITE,
) -> Estimate:
    """Share of simulated baseline/candidate comparisons that end in ``verdict``, with its interval.

    Each replicate draws a fresh suite from ``model``, makes the candidate worse by ``drop`` on a
    random ``affected_fraction`` of scenarios (``drop=0`` is an A/A comparison), simulates
    ``trials`` Bernoulli trials per scenario in each run and applies the full decision procedure.

    Args:
        n_scenarios: Scenarios per suite.
        trials: Trials per scenario in each run.
        drop: True suite pass-rate drop of the candidate, in absolute points.
        replicates: Simulated comparisons.
        config: Alpha, minimum effect and bootstrap resamples to decide with.
        seed: Simulation seed.
        verdict: The verdict whose rate is measured (regression: false alarms or power).
        affected_fraction: Share of scenarios the regression touches.
        model: Distribution of per-scenario pass rates.
    """
    rng = np.random.default_rng(seed)
    hits = 0
    n_affected = max(1, round(affected_fraction * n_scenarios))
    for _ in range(replicates):
        rates = model.sample_pass_rates(n_scenarios, rng)
        affected = np.zeros(n_scenarios, dtype=bool)
        affected[rng.permutation(n_scenarios)[:n_affected]] = True
        candidate_rates = shift_pass_rates(rates, drop, affected)
        baseline = _simulate(rates, trials, rng)
        candidate = _simulate(candidate_rates, trials, rng)
        result = paired_comparison(
            baseline,
            candidate,
            rng=rng,
            resamples=config.bootstrap_resamples,
            confidence=1 - config.alpha,
        )
        decided = decide_verdict(result.delta, result.p_value, config.alpha, config.min_effect)
        hits += decided is verdict
    return wilson_estimate(hits, replicates)


def _simulate(rates: FloatArray, trials: int, rng: np.random.Generator) -> ScenarioCounts:
    return ScenarioCounts.from_arrays(np.full(rates.size, trials), rng.binomial(trials, rates))
