"""Synthetic trials for grader tests: an event log builder and a ``GradeContext`` factory."""

import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue, TypeAdapter

from hecklr_schemas import (
    Actor,
    AudioSpanPayload,
    Channel,
    EmailMessage,
    EmailPayload,
    Event,
    EventId,
    EventPayload,
    GradeContext,
    InterruptPayload,
    LLMProvider,
    Party,
    SilencePayload,
    SilenceReason,
    ToolCallPayload,
    ToolResultPayload,
    TrialId,
    UtterancePayload,
    new_id,
)
from hecklr_schemas.ids import RunId
from hecklr_schemas.persona import Persona
from hecklr_schemas.runtime import Trial
from hecklr_schemas.scenario import ContainsCriterion, Scenario, SuccessCriterion
from hecklr_schemas.world import WorldSnapshot

CRITERION: TypeAdapter[SuccessCriterion] = TypeAdapter(SuccessCriterion)


class EventLog:
    """Appends events to one synthetic trial, numbering ``seq`` and turns like a real sink."""

    def __init__(self) -> None:
        self.trial_id = TrialId(new_id())
        self.events: list[Event] = []
        self._turn = 0
        self._calls = 0

    def add(self, actor: Actor, payload: EventPayload, t_ms: int) -> Event:
        event = Event(
            id=EventId(new_id()),
            trial_id=self.trial_id,
            seq=len(self.events),
            t_ms=t_ms,
            actor=actor,
            payload=payload,
        )
        self.events.append(event)
        return event

    def say(
        self,
        actor: Party,
        text: str,
        t_ms: int = 0,
        *,
        end_ms: int | None = None,
        latency_ms: int | None = None,
    ) -> Event:
        turn = self._turn
        self._turn += 1
        payload = UtterancePayload(text=text, turn=turn, end_ms=end_ms, latency_ms=latency_ms)
        return self.add(actor, payload, t_ms)

    def call(
        self,
        tool: str,
        args: dict[str, JsonValue] | None = None,
        t_ms: int = 0,
        *,
        result: JsonValue = None,
        error: str | None = None,
    ) -> tuple[Event, Event]:
        self._calls += 1
        call_id = f"call-{self._calls}"
        call = self.add("agent", ToolCallPayload(call_id=call_id, tool=tool, args=args or {}), t_ms)
        returned = self.add(
            "tool",
            ToolResultPayload(call_id=call_id, tool=tool, result=result, error=error, latency_ms=5),
            t_ms + 5,
        )
        return call, returned

    def email(self, actor: Party, message: EmailMessage, t_ms: int = 0) -> Event:
        return self.add(actor, EmailPayload(message=message), t_ms)

    def span(self, actor: Party, start_ms: int, end_ms: int, content: str = "speech") -> Event:
        payload = AudioSpanPayload.model_validate(
            {"start_ms": start_ms, "end_ms": end_ms, "content": content}
        )
        return self.add(actor, payload, start_ms)

    def silence(
        self,
        start_ms: int,
        end_ms: int,
        *,
        attributed_to: Party | None = "agent",
        reason: SilenceReason = "turn_gap",
    ) -> Event:
        payload = SilencePayload(
            start_ms=start_ms, end_ms=end_ms, attributed_to=attributed_to, reason=reason
        )
        return self.add("system", payload, start_ms)

    def interrupt(
        self, t_ms: int, *, style: str = "barge_in", reaction_ms: int | None = None
    ) -> Event:
        payload = InterruptPayload.model_validate(
            {"target": "agent", "style": style, "reaction_ms": reaction_ms}
        )
        return self.add("user", payload, t_ms)


AcmeData = dict[str, dict[str, dict[str, JsonValue]]]


def snapshot(collections: AcmeData, t_ms: int = 0) -> WorldSnapshot:
    return WorldSnapshot(world_id="acme-maintenance", taken_at_ms=t_ms, collections=collections)


ACME: AcmeData = {
    "customers": {
        "C-1042": {
            "customer_id": "C-1042",
            "name": "Camille Durand",
            "postcode": "92000",
            "address": "4 Rue Lepic",
            "phone": "+33 6 12 34 56 78",
            "email": "camille@acme.test",
        },
        "C-2077": {
            "customer_id": "C-2077",
            "name": "Marc Petit",
            "postcode": "75011",
            "address": "18 Rue Oberkampf",
            "phone": "+33 6 98 76 54 32",
            "email": "marc@acme.test",
        },
    },
    "appointments": {"C-1042": {"customer_id": "C-1042", "slot": "2026-10-23T09:00"}},
    "refunds": {},
}


def make_context(
    log: EventLog,
    criterion: SuccessCriterion | dict[str, Any] | None = None,
    *,
    channel: Channel = Channel.CHAT,
    world_before: WorldSnapshot | None = None,
    world_after: WorldSnapshot | None = None,
    facts: dict[str, JsonValue] | None = None,
    llm: LLMProvider | None = None,
    judge_model: str | None = "fake/judge",
    duration_ms: int = 60_000,
    criterion_index: int | None = 0,
    seed: int = 7,
) -> GradeContext:
    """A context for one finished trial made of ``log``'s events."""
    parsed = CRITERION.validate_python(criterion) if isinstance(criterion, dict) else criterion
    before = world_before or snapshot(ACME)
    trial = Trial(
        id=log.trial_id,
        run_id=RunId(new_id()),
        scenario_id="reschedule-boiler-visit",
        persona_id="busy-homeowner",
        channel=channel,
        index=0,
        seed=seed,
        status="ok",
        started_at=datetime(2026, 10, 21, tzinfo=UTC),
        duration_ms=duration_ms,
        cost_usd=Decimal(0),
        world_before=before,
        world_after=world_after or before,
    )
    scenario = Scenario(
        id="reschedule-boiler-visit",
        title="Customer moves a boiler maintenance visit",
        channels=[channel],
        persona="busy-homeowner",
        world="acme-maintenance",
        goal="Move Thursday's visit to next week, mornings only",
        facts={"customer_id": "C-1042", "postcode": "92000"} if facts is None else facts,
        success=[parsed or ContainsCriterion(type="contains", text="x")],
    )
    return GradeContext(
        trial=trial,
        events=list(log.events),
        scenario=scenario,
        persona=Persona(id="busy-homeowner", description="Calls from a car"),
        criterion=parsed,
        criterion_index=criterion_index if parsed is not None else None,
        llm=llm,
        judge_model=judge_model,
    )


ContextFactory = Callable[..., GradeContext]


@pytest.fixture
def log() -> EventLog:
    return EventLog()


@pytest.fixture
def ctx_for() -> ContextFactory:
    return make_context


SnapshotFactory = Callable[..., WorldSnapshot]


@pytest.fixture
def acme() -> AcmeData:
    """A private copy of the acme-maintenance world data, free to modify."""
    return copy.deepcopy(ACME)


@pytest.fixture
def snap() -> SnapshotFactory:
    return snapshot


RECORDED_EMAIL_TRIAL = Path(__file__).parent / "fixtures" / "email-gas-smell-trial.json"
"""A real offline trial of acme-email's ``email-emergency-1`` (rules brain, scripted customer):
the agent opens an emergency ticket and says so by email; email has no ``handoff`` event."""


def recorded_email_context(
    criterion: dict[str, Any], *, llm: LLMProvider | None = None, judge_model: str = "fake/judge"
) -> GradeContext:
    """``RECORDED_EMAIL_TRIAL`` as a context for grading ``criterion``."""
    recorded = json.loads(RECORDED_EMAIL_TRIAL.read_text("utf-8"))
    parsed = CRITERION.validate_python(criterion)
    scenario = Scenario.model_validate(recorded["scenario"]).model_copy(
        update={"success": [parsed]}
    )
    return GradeContext(
        trial=Trial.model_validate(recorded["trial"]),
        events=[Event.model_validate(event) for event in recorded["events"]],
        scenario=scenario,
        persona=Persona.model_validate(recorded["persona"]),
        criterion=parsed,
        criterion_index=0,
        llm=llm,
        judge_model=judge_model,
    )


@pytest.fixture
def email_ctx_for() -> Callable[..., GradeContext]:
    return recorded_email_context
