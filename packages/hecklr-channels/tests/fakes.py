"""Small fakes of the trial-loop Protocols the chat driver consumes (sink, simulators, agents)."""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

import anyio

from hecklr_adapters import AgentError
from hecklr_llm import FakeLLM
from hecklr_schemas import (
    AgentChunk,
    Channel,
    Clock,
    DialogueTurn,
    EndReason,
    Event,
    EventId,
    EventPayload,
    FakeClock,
    LLMRequest,
    Message,
    Persona,
    Scenario,
    SessionContext,
    SessionSummary,
    SimEnd,
    SimTurn,
    TrialBrief,
    TrialId,
    UtterancePayload,
    new_id,
)
from hecklr_schemas.common import Actor

TRIAL_ID = TrialId(new_id())


class ListSink:
    """The trial's event log, on any ``Clock``."""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.events: list[Event] = []

    def now_ms(self) -> int:
        return self.clock.now_ms()

    def emit(self, actor: Actor, payload: EventPayload, *, t_ms: int | None = None) -> Event:
        event = Event(
            id=EventId(new_id()),
            trial_id=TRIAL_ID,
            seq=len(self.events),
            t_ms=self.now_ms() if t_ms is None else t_ms,
            actor=actor,
            payload=payload,
        )
        self.events.append(event)
        return event

    def utterances(self, actor: Actor) -> list[tuple[Event, UtterancePayload]]:
        return [
            (event, event.payload)
            for event in self.events
            if event.actor == actor and isinstance(event.payload, UtterancePayload)
        ]

    def kinds(self) -> list[str]:
        return [f"{event.actor}:{event.kind}" for event in self.events]


def brief(*, max_turns: int = 24, max_duration_s: int = 300) -> TrialBrief:
    scenario = Scenario.model_validate(
        {
            "id": "reschedule",
            "title": "Move a visit",
            "channels": ["chat"],
            "persona": "busy",
            "world": "acme",
            "goal": "Move Thursday's visit",
            "success": [{"type": "judge", "rubric": "resolved"}],
            "limits": {"max_turns": max_turns, "max_duration_s": max_duration_s},
        }
    )
    persona = Persona.model_validate({"id": "busy", "description": "short on time"})
    return TrialBrief(
        trial_id=TRIAL_ID, scenario=scenario, persona=persona, channel=Channel.CHAT, seed=3
    )


def context(timeout_s: float = 60.0) -> SessionContext:
    return SessionContext(trial_id=TRIAL_ID, seed=3, timeout_s=timeout_s)


class ScriptedCustomer:
    """Says ``lines`` in order, then ends with ``end``; records the dialogues it was shown."""

    def __init__(self, lines: Sequence[str], end: SimEnd | None = None) -> None:
        self.lines = list(lines)
        self.end = end or SimEnd(reason=EndReason.GOAL_ACHIEVED)
        self.seen: list[list[DialogueTurn]] = []

    @property
    def contradictions(self) -> int:
        return 0

    async def next_turn(self, dialogue: Sequence[DialogueTurn]) -> SimTurn | SimEnd:
        self.seen.append(list(dialogue))
        said = sum(turn.speaker == "user" for turn in dialogue)
        return SimTurn(text=self.lines[said]) if said < len(self.lines) else self.end


class LLMCustomer:
    """A simulated customer whose every turn is an LLM call; ``[END]`` ends the conversation."""

    def __init__(self, llm: FakeLLM) -> None:
        self.llm = llm

    @property
    def contradictions(self) -> int:
        return 0

    async def next_turn(self, dialogue: Sequence[DialogueTurn]) -> SimTurn | SimEnd:
        transcript = "\n".join(f"{turn.speaker}: {turn.text}" for turn in dialogue) or "(start)"
        request = LLMRequest(
            model="fake/sim", messages=[Message(role="user", content=transcript)], seed=3
        )
        text = (await self.llm.complete(request)).text
        if text == "[END]":
            return SimEnd(reason=EndReason.GOAL_ACHIEVED)
        return SimTurn(text=text)


@dataclass(frozen=True)
class Step:
    """Advance the fake clock by ``advance_ms`` (or sleep ``sleep_s``), then yield ``chunk``."""

    chunk: AgentChunk | None = None
    advance_ms: int = 0
    sleep_s: float = 0.0
    raise_error: AgentError | None = None


@dataclass
class ScriptedAgent:
    """A ``ChatTarget`` whose replies follow per-turn ``Step`` scripts on a fake clock."""

    clock: FakeClock
    turns: list[list[Step]]
    default: list[Step] = field(default_factory=lambda: [Step(AgentChunk(text="ok", final=True))])
    open_error: AgentError | None = None
    open_sleep_s: float = 0.0
    close_error: AgentError | None = None
    close_sleep_s: float = 0.0
    received: list[str] = field(default_factory=list)
    closed: int = 0
    finalized: int = 0
    """Reply streams whose cleanup ran (the driver closed them)."""

    async def open(self, ctx: SessionContext) -> "ScriptedAgent":
        await anyio.sleep(self.open_sleep_s)
        if self.open_error is not None:
            raise self.open_error
        return self

    async def send(self, text: str) -> AsyncIterator[AgentChunk]:
        script = (
            self.turns[len(self.received)] if len(self.received) < len(self.turns) else self.default
        )
        self.received.append(text)
        try:
            for step in script:
                if step.advance_ms:
                    self.clock.advance(step.advance_ms)
                if step.sleep_s:
                    await anyio.sleep(step.sleep_s)
                if step.raise_error is not None:
                    raise step.raise_error
                if step.chunk is not None:
                    yield step.chunk
        finally:
            self.finalized += 1

    async def close(self) -> SessionSummary:
        await anyio.sleep(self.close_sleep_s)
        self.closed += 1
        if self.close_error is not None:
            raise self.close_error
        return SessionSummary(turns=len(self.received), agent_session_id="agent-session")
