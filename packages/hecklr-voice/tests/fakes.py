"""Fakes of the trial-loop Protocols the voice driver consumes, and event helpers."""

from collections.abc import Sequence
from typing import Any

from hecklr_schemas import (
    AudioSpanPayload,
    Channel,
    Clock,
    DialogueTurn,
    EndReason,
    ErrorPayload,
    Event,
    EventId,
    EventPayload,
    InterruptPayload,
    MonotonicClock,
    Persona,
    Scenario,
    SessionContext,
    SilencePayload,
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
    """The trial's event log on a real monotonic clock (the voice driver runs in real time)."""

    def __init__(self, clock: Clock | None = None) -> None:
        self.clock = clock or MonotonicClock()
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

    def interrupts(self) -> list[tuple[Event, InterruptPayload]]:
        return [
            (event, event.payload)
            for event in self.events
            if isinstance(event.payload, InterruptPayload)
        ]

    def silences(self) -> list[SilencePayload]:
        return [event.payload for event in self.events if isinstance(event.payload, SilencePayload)]

    def spans(self, actor: Actor) -> list[AudioSpanPayload]:
        return [
            event.payload
            for event in self.events
            if event.actor == actor and isinstance(event.payload, AudioSpanPayload)
        ]

    def errors(self, source: str) -> list[str]:
        return [
            event.payload.message
            for event in self.events
            if isinstance(event.payload, ErrorPayload) and event.payload.source == source
        ]


def brief(
    *,
    behaviors: dict[str, Any] | None = None,
    voice: dict[str, Any] | None = None,
    max_turns: int = 24,
    max_duration_s: int = 120,
    seed: int = 3,
    with_voice: bool = True,
) -> TrialBrief:
    scenario = Scenario.model_validate(
        {
            "id": "reschedule",
            "title": "Move a visit",
            "channels": ["voice"],
            "persona": "caller",
            "world": "acme",
            "goal": "Move Thursday's visit",
            "success": [{"type": "judge", "rubric": "resolved"}],
            "limits": {"max_turns": max_turns, "max_duration_s": max_duration_s},
        }
    )
    persona_fields: dict[str, Any] = {
        "id": "caller",
        "description": "calls about a visit",
        "behaviors": {"response_gap_ms": [200, 300], **(behaviors or {})},
    }
    if with_voice:
        persona_fields["voice"] = {
            "tts": {"provider": "synthetic", "voice": "caller"},
            **(voice or {}),
        }
    persona = Persona.model_validate(persona_fields)
    return TrialBrief(
        trial_id=TRIAL_ID, scenario=scenario, persona=persona, channel=Channel.VOICE, seed=seed
    )


def context(timeout_s: float = 10.0) -> SessionContext:
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
        # Interjections are user turns too; only the script's own lines advance it.
        said = sum(turn.speaker == "user" and turn.text in self.lines for turn in dialogue)
        return SimTurn(text=self.lines[said]) if said < len(self.lines) else self.end
