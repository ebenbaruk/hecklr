"""Hecklr's LLM judges (``hecklr_grade``) applied to production transcripts.

A judge grades a trial, so a logged conversation is presented as one: its turns and tool calls
become events, and the scenario carries what is known (the customer's goal must be inferred from
the transcript). Ids derive from the conversation id, not the clock, so the judge prompt, and with
it the response cache key, is the same every time the same log is mined.
"""

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

from ulid import ULID

from hecklr_grade import JudgeGrader, load_rubric
from hecklr_schemas import (
    Actor,
    Conversation,
    Event,
    EventId,
    Grade,
    GradeContext,
    JudgeCriterion,
    LLMProvider,
    Persona,
    RunId,
    Scenario,
    ToolCallPayload,
    ToolResultPayload,
    Trial,
    TrialId,
    UtterancePayload,
    WorldSnapshot,
)
from hecklr_sim import derive_subseed

UNKNOWN_GOAL: Final = (
    "Unknown (a real production conversation): infer what the customer wanted from their messages."
)
WORLD_ID: Final = "production"
PERSONA_ID: Final = "production-customer"
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


def stable_ulid(*parts: str | int) -> str:
    """A ULID-shaped id determined by ``parts`` (not by the clock)."""
    digest = hashlib.sha256("\0".join(str(part) for part in parts).encode()).digest()
    return str(ULID.from_bytes(digest[:16]))


def conversation_events(conversation: Conversation, trial_id: TrialId) -> list[Event]:
    """The conversation as trial events: utterances, then each tool call and its result."""
    events: list[Event] = []
    for turn_index, turn in enumerate(conversation.turns):
        t_ms = turn.t_ms if turn.t_ms is not None else turn_index * 1000
        for call_index, call in enumerate(turn.tool_calls):
            call_id = f"{turn_index}.{call_index}"
            request = ToolCallPayload(call_id=call_id, tool=call.tool, args=call.args)
            events.append(_event(conversation, trial_id, events, t_ms, "agent", request))
            result = ToolResultPayload(
                call_id=call_id,
                tool=call.tool,
                result=None if call.error is not None else call.result,
                error=call.error,
                latency_ms=0,
            )
            events.append(_event(conversation, trial_id, events, t_ms, "tool", result))
        if turn.text:
            utterance = UtterancePayload(text=turn.text, turn=turn_index)
            events.append(_event(conversation, trial_id, events, t_ms, turn.role, utterance))
    return events


def _event(
    conversation: Conversation,
    trial_id: TrialId,
    events: Sequence[Event],
    t_ms: int,
    actor: Actor,
    payload: UtterancePayload | ToolCallPayload | ToolResultPayload,
) -> Event:
    seq = len(events)
    return Event(
        id=EventId(stable_ulid(conversation.id, "event", seq)),
        trial_id=trial_id,
        seq=seq,
        t_ms=t_ms,
        actor=actor,
        payload=payload,
    )


def grade_context(
    conversation: Conversation, llm: LLMProvider, judge_model: str, *, seed: int = 0
) -> GradeContext:
    """A ``GradeContext`` that lets ``hecklr_grade`` judges read a production conversation."""
    trial_id = TrialId(stable_ulid(conversation.id, "trial"))
    empty_world = WorldSnapshot(world_id=WORLD_ID, taken_at_ms=0, collections={})
    trial = Trial(
        id=trial_id,
        run_id=RunId(stable_ulid(conversation.id, "run")),
        scenario_id="production",
        persona_id=PERSONA_ID,
        channel=conversation.channel,
        index=0,
        seed=derive_subseed(seed, "judge", conversation.id),
        status="ok",
        started_at=conversation.started_at or _EPOCH,
        duration_ms=max((turn.t_ms or 0) for turn in conversation.turns),
        cost_usd=Decimal(0),
        world_before=empty_world,
        world_after=empty_world,
        turns=len(conversation.turns),
    )
    scenario = Scenario(
        id="production",
        title=f"Production conversation {conversation.id}",
        channels=[conversation.channel],
        persona=PERSONA_ID,
        world=WORLD_ID,
        goal=UNKNOWN_GOAL,
        success=[JudgeCriterion(type="judge", rubric="resolved")],
    )
    return GradeContext(
        trial=trial,
        events=conversation_events(conversation, trial_id),
        scenario=scenario,
        persona=Persona(id=PERSONA_ID, description="A real customer of the agent."),
        llm=llm,
        judge_model=judge_model,
    )


async def judge_conversation(
    conversation: Conversation,
    rubrics: Sequence[str],
    llm: LLMProvider,
    judge_model: str,
    *,
    seed: int = 0,
) -> list[Grade]:
    """One grade per rubric; judge failures come back as ``error`` grades, never as a crash.

    Raises:
        hecklr_grade.UnknownRubricError: A rubric name is not a built-in rubric.
    """
    graders = [JudgeGrader(load_rubric(name)) for name in rubrics]
    ctx = grade_context(conversation, llm, judge_model, seed=seed)
    return [await grader.grade(ctx) for grader in graders]
