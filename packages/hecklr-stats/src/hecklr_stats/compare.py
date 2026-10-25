"""Baseline vs candidate: paired effect, interval, p- and q-values, and the verdict rule.

Comparisons are paired by scenario (spec, "Comparing baseline and candidate"):

- Δ is the mean over scenarios of (candidate pass rate - baseline pass rate).
- Its interval is a paired cluster bootstrap: the same resampled scenarios for both runs, then each
  run's trials resampled within each scenario. When every scenario of both runs passed all or none
  of its trials, the bootstrap has no trial-level noise left and would collapse to the point
  (issue #98); the interval is then a paired-proportion interval on the scenarios
  (``paired_difference_interval``, ADR 00rta).
- Its p-value is a two-sided sign-flip permutation test of the per-scenario differences: exact
  enumeration up to ``EXACT_MAX_SCENARIOS`` scenarios, seeded Monte Carlo above.
- Slices are compared the same way; their p-values are Benjamini-Hochberg adjusted across every
  slice of the run, and slice verdicts use the q-value. The whole-suite comparison is the primary
  hypothesis and is decided on its own p-value (see ``docs/adr/0005b-comparison-decisions.md``).
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import numpy as np
import numpy.typing as npt

from hecklr_schemas import (
    Comparison,
    ComparisonId,
    Estimate,
    IntervalMethod,
    RunId,
    Slice,
    StatsConfig,
    Verdict,
    new_id,
)
from hecklr_stats.intervals import (
    chunk_sizes,
    derive_rng,
    paired_difference_interval,
    percentile_interval,
    z_value,
)
from hecklr_stats.outcomes import (
    FloatArray,
    ScenarioCounts,
    TrialOutcome,
    slice_key,
    slices_of,
)
from hecklr_stats.rates import all_or_nothing_estimate
from hecklr_stats.sequential import group_sequential_boundaries

PASS_RATE = "pass_rate"  # noqa: S105 - a metric name; "pass" here is not a password

EXACT_MAX_SCENARIOS = 16
"""Up to 2**16 = 65,536 sign patterns are enumerated exactly; above that, Monte Carlo."""

TRIALS_NEEDED_POWER = 0.80
"""``trials_needed`` aims for this chance of reaching a decision."""

MAX_TRIALS_NEEDED = 1_000
"""Beyond this many extra trials per scenario, more trials are not a practical way to decide."""

_TIE_TOLERANCE = 1e-12
"""Sign patterns whose statistic equals the observed one up to float rounding count as ties."""


@dataclass(frozen=True, slots=True)
class PairedResult:
    """Paired statistics of one slice, before multiple-testing adjustment.

    Attributes:
        baseline: Baseline suite pass rate over the paired scenarios.
        candidate: Candidate suite pass rate over the paired scenarios.
        delta: Mean per-scenario difference, candidate minus baseline.
        p_value: Two-sided sign-flip permutation p-value of ``delta``.
        delta_se: Bootstrap standard error of ``delta``.
        n_trials_baseline: Baseline trials over the paired scenarios.
        n_trials_candidate: Candidate trials over the paired scenarios.
    """

    baseline: Estimate
    candidate: Estimate
    delta: Estimate
    p_value: float
    delta_se: float
    n_trials_baseline: int
    n_trials_candidate: int

    @property
    def n_scenarios(self) -> int:
        """Scenarios present in both runs."""
        return self.delta.n


def pair(
    baseline: ScenarioCounts, candidate: ScenarioCounts
) -> tuple[ScenarioCounts, ScenarioCounts]:
    """Restrict both runs to the scenarios they share, in the same order."""
    shared = set(baseline.scenario_ids) & set(candidate.scenario_ids)
    return baseline.select(shared), candidate.select(shared)


def paired_comparison(
    baseline: ScenarioCounts,
    candidate: ScenarioCounts,
    *,
    rng: np.random.Generator,
    resamples: int,
    confidence: float,
) -> PairedResult:
    """Δ, its paired cluster bootstrap interval and sign-flip p-value for paired counts."""
    if baseline.scenario_ids != candidate.scenario_ids:
        raise ValueError("baseline and candidate must cover the same scenarios; use pair()")
    if baseline.n_scenarios == 0:
        raise ValueError("no scenarios in common between baseline and candidate")
    differences = candidate.rates - baseline.rates
    base_reps, cand_reps = _paired_bootstrap(baseline, candidate, rng, resamples)
    delta_reps = cand_reps - base_reps
    p_value = sign_flip_p_value(differences, rng=rng, resamples=resamples)
    n = baseline.n_scenarios
    run_estimates = [
        # Each run's own pass rate gets the suite interval's boundary rule (ADR 0005c).
        all_or_nothing_estimate(counts, counts.rates, confidence)
        or _bootstrap_estimate(
            float(np.mean(counts.rates)), reps, n, confidence, IntervalMethod.CLUSTER_BOOTSTRAP
        )
        for counts, reps in ((baseline, base_reps), (candidate, cand_reps))
    ]
    delta = all_or_nothing_delta(baseline, candidate, confidence) or _bootstrap_estimate(
        float(np.mean(differences)),
        delta_reps,
        n,
        confidence,
        IntervalMethod.PAIRED_CLUSTER_BOOTSTRAP,
    )
    return PairedResult(
        baseline=run_estimates[0],
        candidate=run_estimates[1],
        delta=delta,
        p_value=p_value,
        delta_se=float(np.std(delta_reps)),
        n_trials_baseline=int(baseline.trials.sum()),
        n_trials_candidate=int(candidate.trials.sum()),
    )


def all_or_nothing_delta(
    baseline: ScenarioCounts, candidate: ScenarioCounts, confidence: float
) -> Estimate | None:
    """Δ with a paired-proportion interval when every scenario of both runs is all-or-nothing.

    Each scenario is then one pass/fail outcome per run, and Δ is the share of scenarios that
    only the candidate passed minus the share only the baseline passed (ADR 00rta). ``None``
    when some scenario of either run is in between, where the paired bootstrap applies.
    """
    rates = (baseline.rates, candidate.rates)
    if not all(np.all((r == 0) | (r == 1)) for r in rates):
        return None
    base_pass, cand_pass = (r == 1 for r in rates)
    gained = int(np.sum(cand_pass & ~base_pass))
    lost = int(np.sum(base_pass & ~cand_pass))
    low, high = paired_difference_interval(gained, lost, baseline.n_scenarios, confidence)
    return Estimate(
        value=(gained - lost) / baseline.n_scenarios,
        ci_low=low,
        ci_high=high,
        confidence=confidence,
        n=baseline.n_scenarios,
        method=IntervalMethod.WILSON,
    )


def _paired_bootstrap(
    baseline: ScenarioCounts,
    candidate: ScenarioCounts,
    rng: np.random.Generator,
    resamples: int,
) -> tuple[FloatArray, FloatArray]:
    """Suite pass-rate replicates of both runs over the same resampled scenarios."""
    n_scenarios = baseline.n_scenarios
    base_parts: list[FloatArray] = []
    cand_parts: list[FloatArray] = []
    for size in chunk_sizes(resamples, 2 * n_scenarios):
        drawn = rng.integers(0, n_scenarios, size=(size, n_scenarios))
        for counts, parts in ((baseline, base_parts), (candidate, cand_parts)):
            trials = counts.trials[drawn]
            parts.append((rng.binomial(trials, counts.rates[drawn]) / trials).mean(axis=1))
    return np.concatenate(base_parts), np.concatenate(cand_parts)


def _bootstrap_estimate(
    point: float, replicates: FloatArray, n: int, confidence: float, method: IntervalMethod
) -> Estimate:
    low, high = percentile_interval(replicates, confidence)
    # Percentile intervals of a mean of discrete rates can exclude the point by a rounding step;
    # widening keeps every reported value inside its own interval.
    return Estimate(
        value=point,
        ci_low=min(low, point),
        ci_high=max(high, point),
        confidence=confidence,
        n=n,
        method=method,
    )


def sign_flip_p_value(
    differences: npt.ArrayLike, *, rng: np.random.Generator, resamples: int
) -> float:
    """Two-sided paired permutation p-value: how often random sign flips match |mean difference|.

    Under no effect each scenario's difference is as likely to be negated, so the observed mean is
    compared with the means of sign-flipped copies. Small suites enumerate every pattern (exact);
    larger ones draw ``resamples`` patterns and use ``(1 + hits) / (1 + resamples)``, which is never
    below the true p-value's resolution and so keeps the test valid.
    """
    values = np.asarray(differences, dtype=np.float64)
    observed = abs(float(values.sum())) - _TIE_TOLERANCE
    if values.size <= EXACT_MAX_SCENARIOS:
        bits = np.arange(2**values.size)[:, None] >> np.arange(values.size)[None, :]
        signs = 1.0 - 2.0 * (bits & 1)
        return float(np.mean(np.abs(signs @ values) >= observed))
    hits = 0
    for size in chunk_sizes(resamples, values.size):
        signs = 1.0 - 2.0 * rng.integers(0, 2, size=(size, values.size))
        hits += int(np.count_nonzero(np.abs(signs @ values) >= observed))
    return (1 + hits) / (1 + resamples)


def benjamini_hochberg(p_values: npt.ArrayLike) -> FloatArray:
    """Benjamini-Hochberg q-values (step-up FDR adjustment), in the input order."""
    p = np.asarray(p_values, dtype=np.float64)
    if p.size == 0:
        return p
    if np.any((p < 0) | (p > 1)):
        raise ValueError("p-values must be in [0, 1]")
    order = np.argsort(p, kind="stable")
    scaled = p[order] * p.size / np.arange(1, p.size + 1)
    q_sorted = np.minimum.accumulate(scaled[::-1])[::-1].clip(max=1.0)
    q = np.empty_like(p)
    q[order] = q_sorted
    return q


def decide_verdict(delta: Estimate, p_value: float, alpha: float, min_effect: float) -> Verdict:
    """The spec's verdict rule, in order: regression, improvement, no change, else inconclusive.

    Args:
        delta: Candidate minus baseline, with its interval.
        p_value: The p-value (or q-value, for slices) tested against ``alpha``.
        alpha: Significance threshold (the nominal one of the current look when adaptive).
        min_effect: Minimum effect δ that counts as a real change.
    """
    if p_value < alpha and delta.value < -min_effect:
        return Verdict.REGRESSION
    if p_value < alpha and delta.value > min_effect:
        return Verdict.IMPROVEMENT
    if -min_effect <= delta.ci_low and delta.ci_high <= min_effect:
        return Verdict.NO_CHANGE
    return Verdict.INCONCLUSIVE


def trials_needed(result: PairedResult, *, alpha: float, min_effect: float) -> int | None:
    """Extra trials per scenario, in each run, likely to turn an inconclusive result into a verdict.

    Assumes the observed Δ is the true one and that its standard error shrinks like
    ``1/sqrt(trials per scenario)``, and asks for ``TRIALS_NEEDED_POWER`` chance of deciding:

    - ``|Δ| > δ`` (aim for regression/improvement): the test must reach ``alpha``, needing
      ``SE <= |Δ| / (z_{1-alpha/2} + z_power)``, and the estimate must land beyond δ, needing
      ``SE <= (|Δ| - δ) / z_power``.
    - ``|Δ| <= δ`` (aim for no change): the interval must fit inside ``[-δ, +δ]``, needing
      ``SE <= (δ - |Δ|) / (z_{1-alpha/2} + z_power)``.

    The assumption ignores between-scenario spread, which more trials cannot remove, so the number
    is a lower bound when scenarios disagree.

    Returns:
        ``None`` when more trials are unlikely to decide: too few scenarios for the permutation test
        to ever reach ``alpha`` (add scenarios), every scenario all-or-nothing in both runs (Δ's
        interval is then on scenarios, ADR 00rta, and only more scenarios narrow it), no
        measurable noise, or more than ``MAX_TRIALS_NEEDED`` trials per scenario.
    """
    if 2.0 ** (1 - result.n_scenarios) >= alpha:
        return None
    if result.delta.method is IntervalMethod.WILSON:
        return None
    size = abs(result.delta.value)
    z_alpha = z_value(1 - alpha)
    z_power = z_value(2 * TRIALS_NEEDED_POWER - 1)
    if size > min_effect:
        target_se = min(size / (z_alpha + z_power), (size - min_effect) / z_power)
    else:
        target_se = (min_effect - size) / (z_alpha + z_power)
    if result.delta_se <= 0 or target_se <= 0:
        return None
    mean_trials = (result.n_trials_baseline + result.n_trials_candidate) / (2 * result.n_scenarios)
    extra = math.ceil(mean_trials * ((result.delta_se / target_se) ** 2 - 1))
    return None if extra > MAX_TRIALS_NEEDED else max(1, extra)


def compare_runs(
    baseline: Sequence[TrialOutcome],
    candidate: Sequence[TrialOutcome],
    *,
    baseline_run: RunId,
    candidate_run: RunId,
    config: StatsConfig,
    seed: int,
    look_fractions: Sequence[float] | None = None,
) -> list[Comparison]:
    """Compare pass rates of two runs on the whole suite and on every slice.

    Args:
        baseline: Outcomes of the baseline run.
        candidate: Outcomes of the candidate run.
        baseline_run: Baseline run id.
        candidate_run: Candidate run id.
        config: Alpha, minimum effect and resamples; ``config.seed`` overrides ``seed`` when set.
        seed: The run seed.
        look_fractions: Information fractions of every look so far, for ``--adaptive``; the last
            one is the current look and sets the significance threshold. ``None`` for a fixed
            design.

    Returns:
        The whole-suite comparison first, then one per slice present in both runs, sorted by key.
    """
    stats_seed = config.seed if config.seed is not None else seed
    confidence = 1 - config.alpha
    threshold, look, alpha_spent = config.alpha, 1, None
    if look_fractions is not None:
        boundary = group_sequential_boundaries(look_fractions, config.alpha)[-1]
        threshold, look, alpha_spent = boundary.nominal_alpha, boundary.look, boundary.alpha_spent

    results: list[tuple[Slice, PairedResult]] = []
    for slice_ in [{}, *slices_of(baseline, candidate)]:
        base_counts, cand_counts = pair(
            ScenarioCounts.from_outcomes(o for o in baseline if o.in_slice(slice_)),
            ScenarioCounts.from_outcomes(o for o in candidate if o.in_slice(slice_)),
        )
        if base_counts.n_scenarios == 0:
            if not slice_:
                raise ValueError("no scenarios in common between baseline and candidate")
            continue
        rng = derive_rng(stats_seed, PASS_RATE, slice_key(slice_))
        result = paired_comparison(
            base_counts,
            cand_counts,
            rng=rng,
            resamples=config.bootstrap_resamples,
            confidence=confidence,
        )
        results.append((slice_, result))

    slice_q = benjamini_hochberg([result.p_value for _, result in results[1:]])
    q_values = [results[0][1].p_value, *slice_q.tolist()]
    created_at = datetime.now(UTC)
    comparisons = []
    for (slice_, result), q_value in zip(results, q_values, strict=True):
        tested = q_value if slice_ else result.p_value
        verdict = decide_verdict(result.delta, tested, threshold, config.min_effect)
        needed = (
            trials_needed(result, alpha=config.alpha, min_effect=config.min_effect)
            if verdict is Verdict.INCONCLUSIVE
            else None
        )
        comparisons.append(
            Comparison(
                id=ComparisonId(new_id()),
                baseline=baseline_run,
                candidate=candidate_run,
                metric=PASS_RATE,
                slice=slice_,
                baseline_estimate=result.baseline,
                candidate_estimate=result.candidate,
                delta=result.delta,
                p_value=result.p_value,
                q_value=q_value,
                verdict=verdict,
                alpha=config.alpha,
                min_effect=config.min_effect,
                n_scenarios=result.n_scenarios,
                n_trials_baseline=result.n_trials_baseline,
                n_trials_candidate=result.n_trials_candidate,
                trials_needed=needed,
                look=look,
                alpha_spent=alpha_spent,
                created_at=created_at,
            )
        )
    return comparisons


DECISIVE: Final = frozenset({Verdict.REGRESSION, Verdict.IMPROVEMENT})
"""Verdicts that cross a group-sequential boundary and so end an adaptive comparison early."""


def compare_sequentially(
    baseline: Sequence[TrialOutcome],
    looks: Sequence[Sequence[TrialOutcome]],
    *,
    baseline_run: RunId,
    candidate_run: RunId,
    config: StatsConfig,
    seed: int,
) -> list[Comparison]:
    """Compare at each look of a group-sequential design and stop at the first decision.

    ``--adaptive`` adds candidate trials in batches; each batch is a look (issue #103). Look j's
    information fraction is its share of the candidate's trials, and it is tested at its
    O'Brien-Fleming nominal alpha (``group_sequential_boundaries``), so looking more than once
    keeps the overall false-alarm rate at ``config.alpha``. The first look whose whole-suite
    verdict is a regression or an improvement decides; otherwise the last look does. No change
    never stops a comparison early: the design spends alpha on rejections only.

    Args:
        baseline: Outcomes of the baseline run.
        looks: The candidate outcomes available at each look, cumulative: each look holds every
            trial of the one before, and the last holds all of them.
        baseline_run: Baseline run id.
        candidate_run: Candidate run id.
        config: Overall alpha, minimum effect and resamples.
        seed: The run seed.

    Returns:
        ``compare_runs`` of the deciding look, with its ``look`` and ``alpha_spent``.

    Raises:
        ValueError: no look, or looks that do not grow by at least ``MIN_LOOK_GAP`` of the trials.
    """
    if not looks:
        raise ValueError("a sequential comparison needs at least one look")
    total = len(looks[-1])
    fractions = [len(look) / total for look in looks]
    group_sequential_boundaries(fractions, config.alpha)  # validates every look up front
    comparisons: list[Comparison] = []
    for number, look in enumerate(looks, start=1):
        comparisons = compare_runs(
            baseline,
            look,
            baseline_run=baseline_run,
            candidate_run=candidate_run,
            config=config,
            seed=seed,
            look_fractions=fractions[:number],
        )
        if comparisons[0].verdict in DECISIVE:
            break
    return comparisons
