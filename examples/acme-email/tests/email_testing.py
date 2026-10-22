"""Test helpers for the acme-email tests; conftest puts this directory on sys.path.

``run_email_trial`` runs one real Hecklr email trial in process, with no server: the scenario's
scripted customer (``hecklr_sim``) writes to the agent through the in-process email adapter and
the email driver (``hecklr_channels``), the agent's tools act on a ``TrialWorld``
(``hecklr_world``) that records every call and injects the scenario's faults, and the scenario's
criteria are graded by ``hecklr_grade``. It is the trial loop ``hecklr run`` executes, minus the
orchestrator and storage.
"""

import contextlib
import socket
from collections.abc import AsyncIterator, Generator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

import anyio
import uvicorn
from acme_agent import AcmeAgent, AgentSettings, ToolOutcome, fault_overlay
from acme_email import EMAIL_ROOT, EmailDesk, InProcessEmailAgent, load_email_agent_files
from fastapi import FastAPI
from pydantic import JsonValue

from hecklr_adapters import AgentSession
from hecklr_adapters.email import PythonEmailTarget
from hecklr_channels import EmailDriver
from hecklr_core.yaml_files import load_model
from hecklr_grade import build_graders, grade_trial, trial_passed
from hecklr_schemas import (
    Channel,
    EmailPayload,
    Event,
    EventId,
    EventPayload,
    FakeClock,
    Grade,
    GradeContext,
    LLMProvider,
    Persona,
    RunId,
    Scenario,
    SessionContext,
    Trial,
    TrialBrief,
    TrialId,
    World,
    new_id,
)
from hecklr_schemas.common import Actor
from hecklr_sim import ScriptedSimulator
from hecklr_world import Sandbox, TrialWorld

SEED: Final = 7
AGENT_ADDR: Final = "support@acme.test"
TEST_TOOL_TIMEOUT_S: Final = 0.2
"""The clean agent's tool deadline in tests, so a hung tool costs a fifth of a second."""
TEST_REPLY_TIMEOUT_S: Final = 2.0
"""How long the driver waits for a reply in tests: well over the tool deadline, so only an agent
that never answers (F12) misses it."""


def email_desk(
    variant: str = "clean",
    *,
    settings: AgentSettings | None = None,
    llm: LLMProvider | None = None,
) -> EmailDesk:
    """An email desk on the clean files or a fault variant, with test deadlines.

    The rules brain by default; ``settings`` with ``brain="llm"`` and ``llm`` for the LLM brain.
    """
    files = load_email_agent_files(None if variant == "clean" else fault_overlay(variant))
    mailer = files.mailer
    if mailer.tool_timeout_s is not None:
        mailer = mailer.model_copy(update={"tool_timeout_s": TEST_TOOL_TIMEOUT_S})
    agent = AcmeAgent(files.brain, settings or AgentSettings(brain="rules"), llm)
    return EmailDesk(agent, mailer, files.variant)


def load_scenarios(pattern: str) -> list[Scenario]:
    """Scenario files of the project matching ``pattern``, sorted by id."""
    scenarios = [load_model(path, Scenario) for path in EMAIL_ROOT.glob(pattern)]
    return sorted(scenarios, key=lambda scenario: scenario.id)


def persona_library() -> dict[str, Persona]:
    """Every persona of the project, by id."""
    paths: list[Path] = sorted(EMAIL_ROOT.glob("personas/**/*.yaml"))
    return {persona.id: persona for persona in (load_model(path, Persona) for path in paths)}


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
class EmailTrial:
    """A graded email trial."""

    trial: Trial
    events: list[Event]
    grades: list[Grade]

    @property
    def passed(self) -> bool:
        """Like the report's verdict: a trial that did not end normally failed."""
        return self.trial.status == "ok" and trial_passed(self.grades)

    def failed(self) -> list[str]:
        """Why the trial failed: its status if not ``ok``, then graders whose grade failed."""
        status = [] if self.trial.status == "ok" else [self.trial.status]
        graders = [g.grader for g in self.grades if g.status == "ok" and not g.passed]
        return status + graders

    def emails(self, actor: Actor) -> list[EmailPayload]:
        return [
            event.payload
            for event in self.events
            if event.actor == actor and isinstance(event.payload, EmailPayload)
        ]


def _brief(
    scenario: Scenario, persona: Persona, trial_id: TrialId | None = None
) -> tuple[TrialBrief, ListSink]:
    trial_id = trial_id or TrialId(new_id())
    brief = TrialBrief(
        trial_id=trial_id, scenario=scenario, persona=persona, channel=Channel.EMAIL, seed=SEED
    )
    return brief, ListSink(trial_id)


async def run_email_trial(
    scenario: Scenario,
    persona: Persona,
    world: World,
    desk: EmailDesk,
    *,
    reply_timeout_s: float = TEST_REPLY_TIMEOUT_S,
) -> EmailTrial:
    """One graded trial of a scripted email scenario against ``desk``, in process."""
    brief, sink = _brief(scenario, persona)
    trial_world = TrialWorld(brief, world, sink)

    def backend(_: AgentSession) -> TrialWorldTools:
        return TrialWorldTools(trial_world)

    async with InProcessEmailAgent(desk, world, backend=backend) as agent:
        target = PythonEmailTarget(agent)
        driver = EmailDriver(target, agent_addr=AGENT_ADDR, reply_timeout_s=reply_timeout_s)
        context = SessionContext(trial_id=brief.trial_id, seed=SEED)
        return await drive_and_grade(brief, sink, trial_world, driver, context)


async def run_served_email_trial(
    scenario: Scenario,
    persona: Persona,
    world: World,
    sandbox: Sandbox,
    driver: EmailDriver,
    *,
    trial_id: TrialId | None = None,
) -> EmailTrial:
    """One graded trial against an agent server, its tools on ``sandbox`` over HTTP."""
    brief, sink = _brief(scenario, persona, trial_id)
    trial_world = await sandbox.open(brief, world, sink)
    context = SessionContext(
        trial_id=brief.trial_id,
        seed=SEED,
        world_http_url=trial_world.http_url,
        world_mcp_url=trial_world.mcp_url,
    )
    return await drive_and_grade(brief, sink, trial_world, driver, context)


async def drive_and_grade(
    brief: TrialBrief,
    sink: ListSink,
    trial_world: TrialWorld,
    driver: EmailDriver,
    context: SessionContext,
) -> EmailTrial:
    """Run the conversation with a scripted customer, close the world and grade the trial."""
    before = trial_world.snapshot()
    result = await driver.run(context, brief, ScriptedSimulator(brief, FakeClock()), sink)
    await trial_world.close()
    after = trial_world.snapshot()
    trial = Trial(
        id=brief.trial_id,
        run_id=RunId(new_id()),
        scenario_id=brief.scenario.id,
        persona_id=brief.persona.id,
        channel=Channel.EMAIL,
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
        error=result.error,
    )
    context_ = GradeContext(
        trial=trial, events=sink.events, scenario=brief.scenario, persona=brief.persona
    )
    grades = await grade_trial(context_, build_graders(brief.scenario))
    return EmailTrial(trial, sink.events, grades)


class _QuietServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to pytest."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[str]:
    """Serve ``app`` (lifespan on) on an ephemeral localhost port for the block."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = _QuietServer(uvicorn.Config(app, log_config=None, access_log=False))
    async with anyio.create_task_group() as group:
        group.start_soon(server.serve, [sock])
        with anyio.fail_after(10):
            while not server.started:  # noqa: ASYNC110 -- uvicorn exposes readiness only as a flag
                await anyio.sleep(0.01)
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
    sock.close()


def free_port() -> int:
    """A localhost port free right now (for servers that need the number before they bind)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])
