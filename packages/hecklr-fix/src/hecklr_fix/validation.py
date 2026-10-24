"""The fixer's two statistical decisions, on ``hecklr_stats.compare_runs`` (ADR 0015a).

Both passes ask about a fixed set of scenarios: "does this patch fix *these* scenarios" and "does
it break *this* suite". The unit is therefore the trial cell (scenario, persona, channel, trial
index), paired between baseline and candidate: both runs use the same seed, so a cell's customer
starts from the same seed in both. Under "the patch changes nothing" each paired difference is
symmetric around zero, so the sign-flip test is valid at the cell level. Pairing by scenario,
as ``hecklr gate`` does to generalize over scenarios, could never reach alpha = 0.05 on the few
scenarios a cluster usually has (ADR 0005b: not with 5 or fewer).

- Targeted pass: keep the candidate when the whole-set verdict is ``improvement``.
- Guard pass: drop it when the whole suite or any tag has a ``regression`` verdict. Slices are
  tags only, so the Benjamini-Hochberg family is exactly "any tag" (spec, "Fixer"). A cell the
  baseline graded but the candidate left without a verdict (harness error, or a judge that
  errored) counts as a failed cell of the candidate: otherwise a patch that breaks scenarios in a
  way that makes their judges error would leave the comparison and pass the guard (issue #99).
"""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from hecklr_core import RunResults
from hecklr_fix.errors import FixError
from hecklr_report.outcomes import convert
from hecklr_schemas import Comparison, Persona, Scenario, StatsConfig, Verdict
from hecklr_stats import TrialOutcome, compare_runs


@dataclass(frozen=True, slots=True)
class SuiteRun:
    """A finished run and the definitions it ran."""

    results: RunResults
    scenarios: Mapping[str, Scenario]
    personas: Mapping[str, Persona]


def cell_outcomes(
    run: SuiteRun, *, failed_if_excluded: Collection[str] = frozenset()
) -> list[TrialOutcome]:
    """One outcome per analyzed trial, keyed by its cell and sliced by its scenario's tags.

    Args:
        run: The run.
        failed_if_excluded: Cells that count as failed when they have no verdict; every other
            harness error and undecided trial is excluded, as everywhere.
    """
    outcomes = []
    for row in convert(run.results, run.scenarios, run.personas).rows:
        cell = f"{row.scenario_id}|{row.persona_id}|{row.channel.value}|{row.index}"
        passed = row.passed
        if passed is None:
            if cell not in failed_if_excluded:
                continue
            passed = False
        scenario = run.scenarios.get(row.scenario_id)
        outcomes.append(
            TrialOutcome(
                scenario_id=cell,
                passed=passed,
                tags=frozenset(scenario.tags) if scenario is not None else frozenset(),
            )
        )
    return outcomes


def compare_cells(
    baseline: SuiteRun,
    candidate: SuiteRun,
    *,
    stats: StatsConfig,
    seed: int,
    guard: bool = False,
) -> list[Comparison]:
    """Paired cell-level comparisons: the whole set first, then one per tag.

    With ``guard``, a candidate cell without a verdict whose baseline cell has one counts as a
    failure (see the module docstring).

    Raises:
        FixError: the runs share no analyzed cell (e.g. every trial was a harness error).
    """
    before = cell_outcomes(baseline)
    graded = {outcome.scenario_id for outcome in before} if guard else frozenset()
    try:
        return compare_runs(
            before,
            cell_outcomes(candidate, failed_if_excluded=graded),
            baseline_run=baseline.results.run.id,
            candidate_run=candidate.results.run.id,
            config=stats,
            seed=seed,
        )
    except ValueError as exc:
        raise FixError(
            f"cannot compare run {candidate.results.run.id} with {baseline.results.run.id}: {exc}"
        ) from exc


def improved(comparisons: Sequence[Comparison]) -> bool:
    """Whether the targeted pass shows a significant improvement on the whole set."""
    return comparisons[0].verdict is Verdict.IMPROVEMENT


def regressions(comparisons: Sequence[Comparison]) -> list[Comparison]:
    """The guard pass's regressions: the whole suite and every tag with that verdict."""
    return [comparison for comparison in comparisons if comparison.verdict is Verdict.REGRESSION]
