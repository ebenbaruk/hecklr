"""Flaky scenario detection (spec, "Validating the statistics").

A scenario whose pass rate sits between 20% and 80% across identical runs is flagged: either the
scenario is ambiguous and should be fixed, or its author accepts the noise knowingly.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from hecklr_schemas import Estimate
from hecklr_stats.intervals import DEFAULT_CONFIDENCE
from hecklr_stats.outcomes import ScenarioCounts, TrialOutcome
from hecklr_stats.rates import scenario_pass_rate

FLAKY_LOW = 0.20
FLAKY_HIGH = 0.80


@dataclass(frozen=True, slots=True)
class FlakyScenario:
    """A scenario flagged as flaky, with its pooled pass rate."""

    scenario_id: str
    pass_rate: Estimate


def flaky_scenarios(
    outcomes: Iterable[TrialOutcome],
    *,
    low: float = FLAKY_LOW,
    high: float = FLAKY_HIGH,
    confidence: float = DEFAULT_CONFIDENCE,
) -> list[FlakyScenario]:
    """Scenarios whose pass rate, pooled over identical runs, lies in ``[low, high]``.

    Args:
        outcomes: Trials of one or more runs of the same agent and configuration.
        low: Lowest pass rate still called flaky.
        high: Highest pass rate still called flaky.
        confidence: Coverage of the Wilson interval reported with each rate.

    Returns:
        Flaky scenarios sorted by id.
    """
    if not 0.0 <= low <= high <= 1.0:
        raise ValueError(f"need 0 <= low <= high <= 1, got {low} and {high}")
    counts = ScenarioCounts.from_outcomes(outcomes)
    return [
        FlakyScenario(scenario_id=sid, pass_rate=scenario_pass_rate(int(c), int(n), confidence))
        for sid, n, c in zip(counts.scenario_ids, counts.trials, counts.passes, strict=True)
        if low <= c / n <= high
    ]
