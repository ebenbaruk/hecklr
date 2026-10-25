"""Stored results to statistics inputs: one ``TrialOutcome`` per analyzable trial.

A trial's verdict follows the ``Grade`` contract (ADR 0004a, ADR 0006):

- harness errors are excluded from statistics and counted;
- agent errors and timeouts are failed trials, whatever their grades say;
- a trial with a failing ``ok`` criterion grade failed, even if another grader errored;
- a trial whose criteria all passed except some that errored has no verdict (``undecided``): it
  is excluded and counted like a harness error, because calling it a pass or a fail would be a
  guess;
- otherwise ``hecklr_grade.trial_passed`` decides.
"""

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from hecklr_core import RunResults
from hecklr_grade import trial_passed
from hecklr_report.model import ExclusionReason, FailureCause, TrialCounts, TrialRow
from hecklr_schemas import (
    Channel,
    Event,
    Grade,
    Persona,
    Scenario,
    SuccessCriterion,
    Trial,
    UtterancePayload,
)
from hecklr_stats import TrialOutcome, wilson_estimate

SNR_BUCKETS: Final = (20, 10, 5, 0)
"""Degradation-curve buckets in dB (spec, "Voice > Metrics"); a level falls in the highest bucket
it reaches, and anything below 0 dB counts as 0."""

CLEAN: Final = "clean"
"""SNR bucket of a voice persona with no background noise."""

CHANNEL, PERSONA, SNR_DB, ACCENT = "channel", "persona", "snr_db", "accent"
"""Slice dimensions read from the trial and its persona (tags are ``hecklr_stats``'s ``tag``)."""

RATIONALE_CHARS: Final = 160


def snr_bucket(snr_db: float | None) -> str:
    """The degradation-curve bucket of a speech-to-noise ratio; ``clean`` when there is no noise."""
    if snr_db is None:
        return CLEAN
    return str(next((bucket for bucket in SNR_BUCKETS if snr_db >= bucket), SNR_BUCKETS[-1]))


def slice_labels(trial: Trial, persona: Persona | None) -> dict[str, str]:
    """Single-valued slice labels: channel and persona, plus SNR bucket and accent for voice."""
    labels = {CHANNEL: trial.channel.value, PERSONA: trial.persona_id}
    voice = persona.voice if persona is not None else None
    if trial.channel is Channel.VOICE and voice is not None:
        labels[SNR_DB] = snr_bucket(voice.snr_db)
        if voice.accent:
            labels[ACCENT] = voice.accent
    return labels


def criterion_label(criterion: SuccessCriterion) -> str:
    """A short, stable name for a success criterion, used to group failures by cause."""
    match criterion.type:
        case "tool_called" | "tool_not_called" | "args_match":
            return f"{criterion.type} {criterion.tool}"
        case "call_order":
            return f"call_order {' > '.join(criterion.tools)}"
        case "judge":
            return f"judge:{criterion.rubric}"
        case "state":
            return f"state: {criterion.description or criterion.expr}"
        case "voice_metric":
            return f"voice:{criterion.metric.value}"
        case "custom":
            return f"custom:{criterion.name}"
        case _:
            return criterion.type


def shorten(text: str | None, limit: int = RATIONALE_CHARS) -> str | None:
    """``text`` on one line, cut to ``limit`` characters with an ellipsis."""
    if text is None:
        return None
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def failure_causes(
    trial: Trial, grades: Sequence[Grade], scenario: Scenario | None
) -> list[FailureCause]:
    """Why ``trial`` failed: its status if it did not end normally, then each failing criterion."""
    causes = []
    if trial.status in ("agent_error", "timeout"):
        causes.append(FailureCause(cause=trial.status, rationale=shorten(trial.error)))
    for grade in grades:
        if grade.criterion_index is None or grade.status != "ok" or grade.passed:
            continue
        criteria = scenario.success if scenario is not None else []
        index = grade.criterion_index
        cause = criterion_label(criteria[index]) if index < len(criteria) else grade.grader
        causes.append(FailureCause(cause=cause, rationale=shorten(grade.rationale)))
    return causes


def trial_verdict(
    trial: Trial, grades: Sequence[Grade]
) -> tuple[bool | None, ExclusionReason | None]:
    """Whether the trial passed, or why it has no verdict (see the module docstring)."""
    if trial.status == "harness_error":
        return None, "harness_error"
    criteria = [g for g in grades if g.criterion_index is not None]
    if trial.status != "ok" or any(g.status == "ok" and not g.passed for g in criteria):
        return False, None
    if any(g.status == "error" for g in criteria):
        return None, "grade_error"
    return trial_passed(grades), None


def agent_latencies(events: Sequence[Event]) -> tuple[int, ...]:
    """Response latency of every agent turn (chat time to first token, voice response latency)."""
    return tuple(
        event.payload.latency_ms
        for event in events
        if event.actor == "agent"
        and isinstance(event.payload, UtterancePayload)
        and event.payload.latency_ms is not None
    )


@dataclass(frozen=True, slots=True)
class ConvertedRun:
    """A stored run in the shapes the analysis needs."""

    rows: list[TrialRow]
    outcomes: list[TrialOutcome]
    """One per analyzed trial, in ``rows`` order."""
    counts: TrialCounts


def convert(
    results: RunResults,
    scenarios: Mapping[str, Scenario],
    personas: Mapping[str, Persona],
) -> ConvertedRun:
    """Turn stored trials, events and grades into trial rows and statistics outcomes."""
    events_by_trial: dict[str, list[Event]] = {}
    for event in results.events:
        events_by_trial.setdefault(event.trial_id, []).append(event)
    grades_by_trial: dict[str, list[Grade]] = {}
    for grade in results.grades:
        grades_by_trial.setdefault(grade.trial_id, []).append(grade)

    rows: list[TrialRow] = []
    outcomes: list[TrialOutcome] = []
    for trial in results.trials:
        grades = grades_by_trial.get(trial.id, [])
        scenario = scenarios.get(trial.scenario_id)
        passed, excluded = trial_verdict(trial, grades)
        labels = slice_labels(trial, personas.get(trial.persona_id))
        row = TrialRow(
            trial_id=trial.id,
            scenario_id=trial.scenario_id,
            persona_id=trial.persona_id,
            channel=trial.channel,
            index=trial.index,
            status=trial.status,
            passed=passed,
            excluded=excluded,
            causes=failure_causes(trial, grades, scenario) if passed is False else [],
            labels=labels,
            duration_ms=trial.duration_ms,
            cost_usd=trial.cost_usd,
        )
        rows.append(row)
        if passed is not None:
            outcomes.append(
                TrialOutcome(
                    scenario_id=trial.scenario_id,
                    passed=passed,
                    labels=labels,
                    tags=frozenset(scenario.tags) if scenario is not None else frozenset(),
                    latencies_ms=agent_latencies(events_by_trial.get(trial.id, [])),
                )
            )
    return ConvertedRun(rows, outcomes, _counts(results, rows))


def _counts(results: RunResults, rows: Sequence[TrialRow]) -> TrialCounts:
    statuses = Counter(trial.status for trial in results.trials)
    # A harness error whose simulator counted a contradiction is usually a conversation the
    # consistency guard gave up on (issue #101): it belongs in the rate, or the worst simulations
    # vanish. A trial does not record why the harness failed, so the label claims no cause.
    after_contradiction = sum(
        1 for t in results.trials if t.status == "harness_error" and t.contradictions > 0
    )
    conversations = [
        t for t in results.trials if t.status != "harness_error" or t.contradictions > 0
    ]
    contradicted = sum(1 for t in conversations if t.contradictions > 0)
    return TrialCounts(
        total=len(results.trials),
        ok=statuses["ok"],
        agent_error=statuses["agent_error"],
        timeout=statuses["timeout"],
        harness_error=statuses["harness_error"],
        undecided=sum(1 for row in rows if row.excluded == "grade_error"),
        grade_errors=sum(1 for grade in results.grades if grade.status == "error"),
        analyzed=sum(1 for row in rows if row.passed is not None),
        errors_after_contradiction=after_contradiction,
        contradiction_rate=(
            wilson_estimate(contradicted, len(conversations)) if conversations else None
        ),
    )
