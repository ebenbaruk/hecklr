from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from hecklr_schemas import (
    Channel,
    Clock,
    Event,
    EventId,
    EventPayload,
    FakeClock,
    Persona,
    Scenario,
    ToolCallPayload,
    ToolFault,
    ToolResultPayload,
    TrialBrief,
    TrialId,
    World,
    new_id,
)
from hecklr_schemas.common import Actor
from hecklr_world import load_world

FIXTURES = Path(__file__).parent / "fixtures"


class ListSink:
    """An in-memory ``EventSink`` on a fake clock, as the orchestrator's sink would behave."""

    def __init__(self, trial_id: str, clock: Clock | None = None) -> None:
        self.trial_id = TrialId(trial_id)
        self.clock = clock or FakeClock()
        self.events: list[Event] = []

    def now_ms(self) -> int:
        return self.clock.now_ms()

    def emit(self, actor: Actor, payload: EventPayload, *, t_ms: int | None = None) -> Event:
        event = Event(
            id=EventId(new_id()),
            trial_id=self.trial_id,
            seq=len(self.events),
            t_ms=self.now_ms() if t_ms is None else t_ms,
            actor=actor,
            payload=payload,
        )
        self.events.append(event)
        return event

    def calls(self) -> list[ToolCallPayload]:
        return [e.payload for e in self.events if isinstance(e.payload, ToolCallPayload)]

    def results(self) -> list[ToolResultPayload]:
        return [e.payload for e in self.events if isinstance(e.payload, ToolResultPayload)]


@pytest.fixture
def make_sink() -> Callable[..., ListSink]:
    """``make_sink(trial_id, clock=None)``: a recording sink for one trial."""
    return ListSink


BriefFactory = Callable[..., TrialBrief]


def _make_brief(*, faults: Sequence[ToolFault | dict[str, Any]] = (), seed: int = 7) -> TrialBrief:
    scenario = Scenario.model_validate(
        {
            "id": "reschedule",
            "title": "Move a visit",
            "channels": ["chat"],
            "persona": "busy-homeowner",
            "world": "example-maintenance",
            "goal": "Move Thursday's visit to next week",
            "success": [{"type": "tool_called", "tool": "reschedule_appointment"}],
            "faults": list(faults),
        }
    )
    persona = Persona(id="busy-homeowner", description="Short on time")
    return TrialBrief(
        trial_id=TrialId(new_id()),
        scenario=scenario,
        persona=persona,
        channel=Channel.CHAT,
        seed=seed,
    )


@pytest.fixture
def make_brief() -> BriefFactory:
    """``make_brief(faults=[...], seed=3)``: a chat trial brief against the example world."""
    return _make_brief


@pytest.fixture
def fixtures_on_path(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the fixtures directory on ``sys.path`` so ``example_tools`` imports."""
    monkeypatch.syspath_prepend(str(FIXTURES))
    return FIXTURES


@pytest.fixture
def example_world(fixtures_on_path: Path) -> World:
    """The example maintenance world with its seed files inlined."""
    return load_world(fixtures_on_path / "example_world.yaml")
