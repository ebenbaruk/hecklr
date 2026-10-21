"""The trial loop: how the orchestrator wires a simulated customer, a world and a channel together.

The orchestrator (``hecklr-core``) owns the loop and talks only to these Protocols; the simulator
(``hecklr-sim``), the world sandbox (``hecklr-world``) and the channel drivers (``hecklr-channels``,
``hecklr-voice``) implement them. One trial, in order (ADR 0006)::

    sink = <the trial's event log>
    world = await sandbox.open(brief, world_def, sink)
    before = world.snapshot()
    ctx = SessionContext(trial_id=brief.trial_id, seed=brief.seed,
                         world_mcp_url=world.mcp_url, world_http_url=world.http_url)
    result = await driver.run(ctx, brief, make_simulator(brief, llm), sink)
    after = world.snapshot()
    await world.close()

Every component writes events through the one ``EventSink``, so a trial has a single ordered log.
"""

from collections.abc import Sequence
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import Field

from hecklr_schemas.adapters import SessionContext, SessionSummary
from hecklr_schemas.common import Actor, Channel, FrozenModel
from hecklr_schemas.events import Event, EventPayload, Party
from hecklr_schemas.ids import TrialIdField
from hecklr_schemas.llm import LLMProvider
from hecklr_schemas.persona import Persona
from hecklr_schemas.runtime import Count, EndReason
from hecklr_schemas.scenario import Scenario
from hecklr_schemas.world import World, WorldSnapshot


@runtime_checkable
class EventSink(Protocol):
    """A trial's append-only event log and its clock.

    The sink assigns ``id``, ``trial_id`` and ``seq``; callers only say who did what, and when.
    """

    def now_ms(self) -> int:
        """Milliseconds since the trial started, on the harness monotonic clock."""
        ...

    def emit(self, actor: Actor, payload: EventPayload, *, t_ms: int | None = None) -> Event:
        """Append an event at ``t_ms`` (default: now) and return it.

        Pass ``t_ms`` when recording something after the fact, e.g. an agent reply logged once
        complete but stamped with its first token.
        """
        ...


class TrialBrief(FrozenModel):
    """Everything fixed about one trial before it starts."""

    trial_id: TrialIdField
    scenario: Scenario
    persona: Persona
    channel: Channel
    seed: int
    """The trial seed; every random choice in the trial derives from it."""


# --- simulated customer (hecklr-sim) ---------------------------------------------------------


class DialogueTurn(FrozenModel):
    """One visible message of the conversation so far, as the customer perceived it.

    Only what the customer could see or hear: never tool calls or other agent internals.
    """

    speaker: Party
    text: str


class SimTurn(FrozenModel):
    """The customer's next message."""

    kind: Literal["turn"] = "turn"
    text: Annotated[str, Field(min_length=1)]
    subject: str | None = None
    """Email only: the subject of the first message; replies reuse the thread's subject."""


class SimEnd(FrozenModel):
    """The customer stops the conversation."""

    kind: Literal["end"] = "end"
    reason: EndReason
    closing_text: str | None = None
    """An optional last message to send before leaving, e.g. "great, thanks, bye"."""


@runtime_checkable
class CustomerSimulator(Protocol):
    """The simulated customer of one trial, created from its ``TrialBrief``."""

    @property
    def contradictions(self) -> int:
        """Turns so far that the self-consistency guard regenerated (reported as ``Trial``)."""
        ...

    async def next_turn(self, dialogue: Sequence[DialogueTurn]) -> SimTurn | SimEnd:
        """Return the customer's next message given the conversation so far, or end it."""
        ...


@runtime_checkable
class SimulatorFactory(Protocol):
    """Builds the simulator for a trial; the simulator model is the factory's configuration."""

    def __call__(self, brief: TrialBrief, llm: LLMProvider) -> CustomerSimulator:
        """Create a simulator that pursues ``brief.scenario`` as ``brief.persona``."""
        ...


# --- world sandbox (hecklr-world) ------------------------------------------------------------


@runtime_checkable
class WorldSession(Protocol):
    """One trial's isolated world: its own store, fault schedule and tool endpoints."""

    @property
    def mcp_url(self) -> str | None:
        """MCP endpoint of this trial's tools; ``None`` in observed mode."""
        ...

    @property
    def http_url(self) -> str | None:
        """OpenAPI endpoint of this trial's tools; ``None`` in observed mode."""
        ...

    def snapshot(self) -> WorldSnapshot:
        """The full state now, stamped with the sink's clock."""
        ...

    async def close(self) -> None:
        """Release the store and endpoints; held (``timeout``) calls are dropped."""
        ...


@runtime_checkable
class WorldSandbox(Protocol):
    """Hosts one isolated world per trial.

    The session writes a ``tool_call`` and a ``tool_result`` event to the sink for every call:
    from the sandbox's own endpoints, or, in observed mode (``agent.tool_mode``), from the agent's
    OpenTelemetry spans carrying the trial's ``X-Hecklr-Trial-Id`` (``source="observed"``).
    """

    async def open(self, brief: TrialBrief, world: World, sink: EventSink) -> WorldSession:
        """Load ``world`` fresh, seed ``brief.scenario.faults`` from ``brief.seed`` and serve it."""
        ...


# --- channel drivers (hecklr-channels, hecklr-voice) -----------------------------------------


class DriverResult(FrozenModel):
    """How the conversation ended, as seen by the channel driver."""

    status: Literal["ok", "agent_error", "timeout"]
    """``timeout``: the agent missed a deadline or the scenario's limits were hit mid-turn."""
    end_reason: EndReason
    turns: Count
    """Turns taken by both parties."""
    error: str | None = None
    """What the agent did wrong, for ``agent_error`` and ``timeout``."""
    summary: SessionSummary | None = None
    """What the adapter reported when the session closed."""


@runtime_checkable
class ChannelDriver(Protocol):
    """Runs one conversation over one channel against the agent's adapter.

    The driver alternates simulator turns and agent replies, enforces ``brief.scenario.limits``,
    and writes every utterance, audio span, interrupt, silence, email and handoff to the sink. It
    raises for harness failures (the orchestrator retries those) and reports agent failures in
    the result.
    """

    @property
    def channel(self) -> Channel:
        """The channel this driver serves."""
        ...

    async def run(
        self,
        ctx: SessionContext,
        brief: TrialBrief,
        simulator: CustomerSimulator,
        sink: EventSink,
    ) -> DriverResult:
        """Run the conversation to its end and say how it ended."""
        ...
