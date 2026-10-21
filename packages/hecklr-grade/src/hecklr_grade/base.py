"""Building blocks shared by every grader: grade construction and event queries."""

import math
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Literal

from hecklr_schemas import (
    EmailPayload,
    Event,
    EventRef,
    Grade,
    GradeContext,
    GradeId,
    Grader,
    Party,
    UtterancePayload,
    new_id,
)
from hecklr_schemas.runtime import JudgeVote


def make_grade(
    ctx: GradeContext,
    grader: Grader,
    *,
    passed: bool,
    value: bool | float | None = None,
    rationale: str | None = None,
    evidence: Iterable[Event] = (),
    status: Literal["ok", "skipped", "error"] = "ok",
    judge_model: str | None = None,
    votes: Sequence[JudgeVote] = (),
    cost_usd: Decimal = Decimal(0),
) -> Grade:
    """Build the ``Grade`` that ``grader`` gives the trial in ``ctx``.

    ``value`` defaults to ``passed``; ``evidence`` events become ``EventRef``s on the trial.
    """
    return Grade(
        id=GradeId(new_id()),
        trial_id=ctx.trial.id,
        grader=grader.name,
        grader_version=grader.version,
        criterion_index=ctx.criterion_index,
        status=status,
        value=passed if value is None else value,
        passed=passed,
        rationale=rationale,
        evidence=refs(ctx, evidence),
        judge_model=judge_model,
        votes=list(votes),
        cost_usd=cost_usd,
    )


def skipped(ctx: GradeContext, grader: Grader, reason: str) -> Grade:
    """A grade saying the grader does not apply to this trial (e.g. voice metric on chat)."""
    return make_grade(ctx, grader, passed=False, rationale=reason, status="skipped")


def errored(
    ctx: GradeContext,
    grader: Grader,
    reason: str,
    *,
    judge_model: str | None = None,
    cost_usd: Decimal = Decimal(0),
) -> Grade:
    """A grade saying the grader could not reach a verdict; never counts as a pass."""
    return make_grade(
        ctx,
        grader,
        passed=False,
        rationale=reason,
        status="error",
        judge_model=judge_model,
        cost_usd=cost_usd,
    )


def criterion_as[T](ctx: GradeContext, kind: type[T]) -> T:
    """The context's criterion, checked to be ``kind``; a mismatch is a wiring bug, so it raises."""
    if not isinstance(ctx.criterion, kind):
        raise TypeError(f"expected a {kind.__name__} criterion, got {type(ctx.criterion).__name__}")
    return ctx.criterion


def refs(ctx: GradeContext, events: Iterable[Event]) -> list[EventRef]:
    """Evidence pointers to ``events`` of the graded trial."""
    return [EventRef(trial_id=ctx.trial.id, event_id=event.id) for event in events]


def timeline(events: Iterable[Event]) -> list[Event]:
    """Events in time order; ``seq`` breaks ties (ADR 0006: ``seq`` alone is append order)."""
    return sorted(events, key=lambda event: (event.t_ms, event.seq))


def of_kind[P](events: Iterable[Event], payload_type: type[P]) -> list[tuple[Event, P]]:
    """``(event, payload)`` pairs whose payload is a ``payload_type``, in time order."""
    return [
        (event, event.payload)
        for event in timeline(events)
        if isinstance(event.payload, payload_type)
    ]


def messages(events: Iterable[Event], speaker: Party) -> list[tuple[Event, str]]:
    """What ``speaker`` said or wrote: utterance text, and email subject plus body."""
    said: list[tuple[Event, str]] = []
    for event in timeline(events):
        if event.actor != speaker:
            continue
        payload = event.payload
        if isinstance(payload, UtterancePayload):
            said.append((event, payload.text))
        elif isinstance(payload, EmailPayload):
            said.append((event, f"{payload.message.subject}\n\n{payload.message.body}"))
    return said


def quantile(values: Sequence[float], q: float) -> float:
    """Nearest-rank quantile: the smallest value with at least ``q`` of the values at or below it.

    Nearest rank always returns an observed value, so a p95 over a handful of turns is a real
    turn's latency rather than an interpolation between two of them.
    """
    if not values:
        raise ValueError("quantile of no values")
    if not 0.0 < q <= 1.0:
        raise ValueError(f"quantile {q} is outside (0, 1]")
    ordered = sorted(values)
    return ordered[math.ceil(q * len(ordered)) - 1]
