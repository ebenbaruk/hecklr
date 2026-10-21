"""Deterministic graders over what was said: free, exact, and run first."""

import re

from hecklr_grade.base import (
    criterion_as,
    errored,
    make_grade,
    messages,
    of_kind,
    quantile,
    skipped,
    timeline,
)
from hecklr_schemas import (
    Channel,
    EmailPayload,
    Event,
    Grade,
    GradeContext,
    Party,
    UtterancePayload,
)
from hecklr_schemas.scenario import (
    ContainsCriterion,
    LatencyP95BelowCriterion,
    MaxTurnsCriterion,
    NotContainsCriterion,
    RegexCriterion,
)


def _messages_containing(
    ctx: GradeContext, speaker: Party, text: str, *, case_sensitive: bool
) -> list[Event]:
    needle = text if case_sensitive else text.casefold()
    return [
        event
        for event, said in messages(ctx.events, speaker)
        if needle in (said if case_sensitive else said.casefold())
    ]


class ContainsGrader:
    """``contains``: some message from the speaker contains the text."""

    name = "contains"
    version = "1"

    async def grade(self, ctx: GradeContext) -> Grade:
        """Pass when at least one message of the speaker contains the text."""
        criterion = criterion_as(ctx, ContainsCriterion)
        hits = _messages_containing(
            ctx, criterion.speaker, criterion.text, case_sensitive=criterion.case_sensitive
        )
        verdict = f"found in {len(hits)} message(s)" if hits else "never said"
        return make_grade(
            ctx,
            self,
            passed=bool(hits),
            rationale=f"{criterion.speaker} text {criterion.text!r}: {verdict}",
            evidence=hits,
        )


class NotContainsGrader:
    """``not_contains``: no message from the speaker contains the text."""

    name = "not_contains"
    version = "1"

    async def grade(self, ctx: GradeContext) -> Grade:
        """Fail if any message of the speaker contains the text, citing every such message."""
        criterion = criterion_as(ctx, NotContainsCriterion)
        hits = _messages_containing(
            ctx, criterion.speaker, criterion.text, case_sensitive=criterion.case_sensitive
        )
        verdict = f"said in {len(hits)} message(s)" if hits else "never said"
        return make_grade(
            ctx,
            self,
            passed=not hits,
            rationale=f"{criterion.speaker} text {criterion.text!r}: {verdict}",
            evidence=hits,
        )


class RegexGrader:
    """``regex``: some message from the speaker matches the pattern (searched, not anchored)."""

    name = "regex"
    version = "1"

    async def grade(self, ctx: GradeContext) -> Grade:
        """Pass when the pattern is found in at least one message of the speaker."""
        criterion = criterion_as(ctx, RegexCriterion)
        try:
            pattern = re.compile(criterion.pattern, re.IGNORECASE if criterion.ignore_case else 0)
        except re.error as exc:
            return errored(ctx, self, f"invalid regex {criterion.pattern!r}: {exc}")
        hits = [
            event for event, said in messages(ctx.events, criterion.speaker) if pattern.search(said)
        ]
        verdict = f"matched {len(hits)} message(s)" if hits else "no match"
        return make_grade(
            ctx,
            self,
            passed=bool(hits),
            rationale=f"{criterion.speaker} /{criterion.pattern}/: {verdict}",
            evidence=hits,
        )


class MaxTurnsGrader:
    """``max_turns``: the agent spoke (or wrote) at most ``limit`` times."""

    name = "max_turns"
    version = "1"

    async def grade(self, ctx: GradeContext) -> Grade:
        """Count agent utterances and emails; the value is that count."""
        criterion = criterion_as(ctx, MaxTurnsCriterion)
        agent_turns = [
            event
            for event in timeline(ctx.events)
            if event.actor == "agent" and isinstance(event.payload, UtterancePayload | EmailPayload)
        ]
        count = len(agent_turns)
        return make_grade(
            ctx,
            self,
            passed=count <= criterion.limit,
            value=float(count),
            rationale=f"{count} agent turn(s), limit {criterion.limit}",
            # The turns past the limit are what went wrong.
            evidence=agent_turns[criterion.limit :],
        )


class LatencyP95BelowGrader:
    """``latency_p95_below``: p95 of agent ``latency_ms`` is below the threshold.

    ``latency_ms`` is chat time to first token and voice response latency (ADR 0006).
    """

    name = "latency_p95_below"
    version = "1"

    async def grade(self, ctx: GradeContext) -> Grade:
        """Nearest-rank p95 over the agent turns that carry a latency."""
        criterion = criterion_as(ctx, LatencyP95BelowCriterion)
        if ctx.trial.channel is Channel.EMAIL:
            return skipped(ctx, self, "response latency is not measured on email")
        timed = [
            (event, payload.latency_ms)
            for event, payload in of_kind(ctx.events, UtterancePayload)
            if event.actor == "agent" and payload.latency_ms is not None
        ]
        if not timed:
            return skipped(ctx, self, "no agent turn with a measured latency")
        p95 = quantile([float(latency) for _, latency in timed], 0.95)
        return make_grade(
            ctx,
            self,
            passed=p95 < criterion.ms,
            value=p95,
            rationale=(
                f"p95 latency {p95:.0f} ms over {len(timed)} turn(s), limit {criterion.ms} ms"
            ),
            evidence=[event for event, latency in timed if latency >= criterion.ms],
        )
