"""Stored trials to ``hecklr_stats.TrialOutcome``: what the run summary and ``--adaptive`` read.

The rule follows the schemas: harness errors are excluded from statistics, and so are trials with
an ``error`` grade on a success criterion (no verdict could be reached), each counted and shown.
Agent errors and timeouts are failed trials. A trial passes by ``hecklr_grade.trial_passed``.
The full analysis with slices lives in ``hecklr-report``; this is the minimum ``hecklr run`` needs.
"""

from collections import defaultdict
from dataclasses import dataclass

from hecklr_core import RunResults
from hecklr_grade import trial_passed
from hecklr_schemas import Grade, UtterancePayload
from hecklr_stats import TrialOutcome


@dataclass(frozen=True, slots=True)
class RunOutcomes:
    """The trials that count, and how many were left out and why."""

    outcomes: list[TrialOutcome]
    harness_errors: int
    """Trials the harness could not run (after retries)."""
    grading_errors: int
    """Trials a success-criterion grader could not decide."""


def trial_outcomes(results: RunResults) -> RunOutcomes:
    """Turn a stored run into the outcomes its statistics are computed from."""
    outcomes: list[TrialOutcome] = []
    harness_errors = grading_errors = 0
    # Grouped once: RunResults.events_of scans every event, which is quadratic over a run.
    grades_by_trial: dict[str, list[Grade]] = defaultdict(list)
    for grade in results.grades:
        grades_by_trial[grade.trial_id].append(grade)
    latencies_by_trial: dict[str, list[int]] = defaultdict(list)
    for event in results.events:
        payload = event.payload
        if (
            event.actor == "agent"
            and isinstance(payload, UtterancePayload)
            and payload.latency_ms is not None
        ):
            latencies_by_trial[event.trial_id].append(payload.latency_ms)
    for trial in results.trials:
        if trial.status == "harness_error":
            harness_errors += 1
            continue
        grades = grades_by_trial[trial.id]
        if any(g.status == "error" and g.criterion_index is not None for g in grades):
            grading_errors += 1
            continue
        outcomes.append(
            TrialOutcome(
                scenario_id=trial.scenario_id,
                passed=trial_passed(grades),
                latencies_ms=tuple(latencies_by_trial[trial.id]),
            )
        )
    return RunOutcomes(outcomes, harness_errors, grading_errors)
