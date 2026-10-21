"""Test helpers for the acme-maintenance tests; conftest puts this directory on sys.path.

``run_scripted_trial`` runs one real Hecklr trial in process, with no server: the scenario's
scripted customer (``hecklr_sim``) talks to the agent through the ``python`` adapter and the chat
driver (``hecklr_channels``), the agent's tools act on a ``TrialWorld`` (``hecklr_world``) that
records every call, and the scenario's success criteria are graded by ``hecklr_grade``. It is the
trial loop ``hecklr run`` executes, minus the orchestrator and storage.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from acme_agent import (
    PROJECT_ROOT,
    AcmeAgent,
    AgentFiles,
    AgentSettings,
    InProcessAgent,
    ToolOutcome,
    fault_overlay,
    load_agent_files,
)
from acme_agent.tools import LocalWorldTools, Reply
from pydantic import JsonValue

from hecklr_adapters import AgentSession, PythonChatTarget
from hecklr_channels import ChatDriver
from hecklr_core.yaml_files import load_model
from hecklr_grade import build_graders, grade_trial, trial_passed
from hecklr_schemas import (
    Channel,
    Event,
    EventId,
    EventPayload,
    FakeClock,
    Grade,
    GradeContext,
    Persona,
    RunId,
    Scenario,
    SessionContext,
    Trial,
    TrialBrief,
    TrialId,
    UtterancePayload,
    World,
    new_id,
)
from hecklr_schemas.common import Actor
from hecklr_sim import ScriptedSimulator
from hecklr_world import TrialWorld

FAULTS: Final = ("F1", "F2", "F3", "F4", "F5", "F6")
SEED: Final = 7


def files_for(variant: str) -> AgentFiles:
    """The clean files (``"clean"``) or a fault variant's."""
    return load_agent_files(overlay=None if variant == "clean" else fault_overlay(variant))


def rules_agent(variant: str = "clean") -> AcmeAgent:
    """A rules-brain agent running the clean files or a fault variant."""
    return AcmeAgent(files_for(variant), AgentSettings(brain="rules"))


def load_scenarios(pattern: str) -> list[Scenario]:
    """Scenario files under the project matching ``pattern``, sorted by id."""
    scenarios = [load_model(path, Scenario) for path in PROJECT_ROOT.glob(pattern)]
    return sorted(scenarios, key=lambda scenario: scenario.id)


class Chat:
    """One conversation with an agent on a private local world, as a customer would have it."""

    def __init__(self, agent: AcmeAgent, world: World) -> None:
        self.agent = agent
        self.tools = LocalWorldTools(world)
        self.replies: list[Reply] = []
        self._seeded = {name: set(records) for name, records in self.tools.state().items()}

    async def say(self, text: str) -> str:
        reply = await self.agent.respond("session-1", text, lambda: self.tools)
        self.replies.append(reply)
        return reply.text

    async def script(self, *texts: str) -> list[str]:
        return [await self.say(text) for text in texts]

    def calls(self, tool: str) -> list[dict[str, JsonValue]]:
        """Arguments of every call to ``tool`` so far."""
        return [
            dict(step.call.args)
            for reply in self.replies
            for step in reply.steps
            if step.call.tool == tool
        ]

    def tools_called(self) -> list[str]:
        return [step.call.tool for reply in self.replies for step in reply.steps]

    def record(self, collection: str, key: str) -> dict[str, JsonValue]:
        return self.tools.state()[collection][key]

    def created(self, collection: str) -> list[dict[str, JsonValue]]:
        records = self.tools.state()[collection]
        return [record for key, record in records.items() if key not in self._seeded[collection]]


class ListSink:
    """An in-memory ``EventSink`` on a fake clock."""

    def __init__(self, trial_id: TrialId) -> None:
        self.trial_id = trial_id
        self.clock = FakeClock()
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


class TrialWorldTools:
    """The agent's tools on a ``TrialWorld`` directly: what the sandbox server would do."""

    reports_calls = False

    def __init__(self, world: TrialWorld) -> None:
        self._world = world

    async def call(self, name: str, args: Mapping[str, JsonValue]) -> ToolOutcome:
        outcome = await self._world.call_tool(name, args)
        return ToolOutcome(ok=outcome.ok, body=outcome.body())


@dataclass(frozen=True)
class TrialOutcome:
    """A graded trial."""

    trial: Trial
    events: list[Event]
    grades: list[Grade]

    @property
    def passed(self) -> bool:
        return trial_passed(self.grades)

    def failed(self) -> list[str]:
        """Graders whose ``ok`` grade failed."""
        return [grade.grader for grade in self.grades if grade.status == "ok" and not grade.passed]

    def transcript(self) -> str:
        return "\n".join(
            f"{event.actor}: {event.payload.text}"
            for event in self.events
            if isinstance(event.payload, UtterancePayload)
        )


async def run_scripted_trial(
    scenario: Scenario, persona: Persona, world: World, agent: AcmeAgent
) -> TrialOutcome:
    """One graded trial of a scripted scenario against ``agent``."""
    trial_id = TrialId(new_id())
    brief = TrialBrief(
        trial_id=trial_id, scenario=scenario, persona=persona, channel=Channel.CHAT, seed=SEED
    )
    sink = ListSink(trial_id)
    trial_world = TrialWorld(brief, world, sink)
    before = trial_world.snapshot()

    def backend(_: AgentSession) -> TrialWorldTools:
        return TrialWorldTools(trial_world)

    async with InProcessAgent(agent, world, backend=backend) as chat:
        result = await ChatDriver(PythonChatTarget(chat)).run(
            SessionContext(trial_id=trial_id, seed=SEED),
            brief,
            ScriptedSimulator(brief, FakeClock()),
            sink,
        )
    after = trial_world.snapshot()
    await trial_world.close()
    trial = Trial(
        id=trial_id,
        run_id=RunId(new_id()),
        scenario_id=scenario.id,
        persona_id=persona.id,
        channel=Channel.CHAT,
        index=0,
        seed=SEED,
        status=result.status,
        end_reason=result.end_reason,
        started_at=datetime.now(UTC),
        duration_ms=sink.now_ms(),
        cost_usd=Decimal(0),
        world_before=before,
        world_after=after,
        turns=result.turns,
    )
    context = GradeContext(trial=trial, events=sink.events, scenario=scenario, persona=persona)
    grades = await grade_trial(context, build_graders(scenario))
    return TrialOutcome(trial, sink.events, grades)


def persona_library() -> dict[str, Persona]:
    """Every persona of the project, by id."""
    paths: list[Path] = sorted(PROJECT_ROOT.glob("personas/**/*.yaml"))
    return {persona.id: persona for persona in (load_model(path, Persona) for path in paths)}
