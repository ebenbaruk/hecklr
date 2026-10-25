"""Synthetic Bernoulli suites for validating the statistics (spec, "Validating the statistics").

A/A false-alarm rate (SC5) and power (SC6) are properties of the whole decision procedure on
realistic suites, so they are measured by simulation rather than assumed. Real suites are
heterogeneous: most scenarios almost always pass, some usually do, a few are hard or flaky.
``REALISTIC_SUITE`` encodes that as a mixture of Beta distributions of per-scenario pass rates.
"""

import math
from collections.abc import Collection
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt
from scipy.optimize import brentq
from scipy.special import expit, logit

from hecklr_schemas import Estimate, RunId, StatsConfig, Verdict
from hecklr_stats.compare import compare_sequentially, decide_verdict, paired_comparison
from hecklr_stats.intervals import wilson_estimate
from hecklr_stats.outcomes import FloatArray, ScenarioCounts, TrialOutcome
from hecklr_stats.sequential import allocate_trials

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


ADAPTIVE_EXTRA_SHARE = 0.5
"""Extra trials of an adaptive pass, as a share of the first pass's (``hecklr run --adaptive``)."""

_SIM_RUN = RunId("0" * 26)


def adaptive_verdict_rate(
    *,
    n_scenarios: int,
    trials: int,
    drop: float,
    replicates: int,
    config: StatsConfig,
    seed: int,
    verdicts: Collection[Verdict] = (Verdict.REGRESSION,),
    model: SuiteModel = REALISTIC_SUITE,
) -> Estimate:
    """Share of simulated adaptive comparisons that end in one of ``verdicts`` (issue #103).

    Each replicate runs the whole adaptive procedure: a first pass of ``trials`` trials per
    scenario in both runs, ``ADAPTIVE_EXTRA_SHARE`` more candidate trials spread by
    ``allocate_trials`` from what the first pass showed, then ``compare_sequentially`` with one
    look per batch. The allocation depends on the data, which the boundaries do not model, so
    this is how the design's false-alarm rate is checked, not assumed.

    Args:
        n_scenarios: Scenarios per suite.
        trials: First-pass trials per scenario in each run.
        drop: True pass-rate drop of the candidate on every scenario, in absolute points.
        replicates: Simulated comparisons.
        config: Alpha, minimum effect and resamples to decide with.
        seed: Simulation seed.
        verdicts: The suite verdicts counted (both rejections, for the test's false-alarm rate).
        model: Distribution of per-scenario pass rates.
    """
    rng = np.random.default_rng(seed)
    hits = 0
    affected = np.ones(n_scenarios, dtype=bool)
    budget = math.ceil(ADAPTIVE_EXTRA_SHARE * n_scenarios * trials)
    for replicate in range(replicates):
        rates = model.sample_pass_rates(n_scenarios, rng)
        candidate_rates = shift_pass_rates(rates, drop, affected)
        baseline = _simulate(rates, trials, rng)
        first = _simulate(candidate_rates, trials, rng)
        extra = allocate_trials(baseline, first, budget)
        extra_trials = np.array([extra.get(sid, 0) for sid in first.scenario_ids])
        added = ScenarioCounts.from_arrays(
            np.maximum(extra_trials, 1), rng.binomial(extra_trials, candidate_rates)
        )
        first_outcomes = _outcomes(first)
        looks = [first_outcomes, first_outcomes + _outcomes(added, extra_trials)]
        comparisons = compare_sequentially(
            _outcomes(baseline),
            looks if budget else looks[:1],
            baseline_run=_SIM_RUN,
            candidate_run=_SIM_RUN,
            config=config,
            seed=seed + replicate,
        )
        hits += comparisons[0].verdict in verdicts
    return wilson_estimate(hits, replicates)


def _outcomes(counts: ScenarioCounts, trials: npt.ArrayLike | None = None) -> list[TrialOutcome]:
    """``counts`` as trial outcomes; ``trials`` overrides how many trials each scenario has."""
    sizes = counts.trials if trials is None else np.asarray(trials)
    return [
        TrialOutcome(scenario_id=sid, passed=i < passes)
        for sid, n, passes in zip(counts.scenario_ids, sizes, counts.passes, strict=True)
        for i in range(int(n))
    ]


def _simulate(rates: FloatArray, trials: int, rng: np.random.Generator) -> ScenarioCounts:
    return ScenarioCounts.from_arrays(np.full(rates.size, trials), rng.binomial(trials, rates))
