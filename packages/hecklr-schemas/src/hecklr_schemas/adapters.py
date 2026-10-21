"""Agent adapter Protocols: the only code that knows how to reach a specific agent.

Everything upstream talks to these Protocols, so a new framework is supported by writing one
adapter and passing the shared contract suite in ``hecklr-adapters/tests/contract/``.
"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final, Protocol, runtime_checkable

from pydantic import Field

from hecklr_schemas.common import FrozenModel
from hecklr_schemas.events import (
    EmailMessage,
    HandoffPayload,
    ToolCallPayload,
    ToolResultPayload,
)
from hecklr_schemas.ids import TrialIdField

TRIAL_ID_HEADER: Final = "X-Hecklr-Trial-Id"
"""Correlation header; agents that emit OpenTelemetry spans echo it so spans join to trials."""


class SessionContext(FrozenModel):
    """What an adapter needs to open one trial's session with the agent."""

    trial_id: TrialIdField
    seed: int
    world_mcp_url: str | None = None
    """MCP endpoint of this trial's world sandbox, if the agent's tools point at it."""
    world_http_url: str | None = None
    """OpenAPI endpoint of this trial's world sandbox."""
    timeout_s: float = Field(default=60.0, gt=0)
    """Per-turn deadline for the agent's reply."""

    def headers(self) -> dict[str, str]:
        """HTTP headers every adapter request must carry."""
        return {TRIAL_ID_HEADER: self.trial_id}


class AgentChunk(FrozenModel):
    """One streamed piece of an agent turn: a text delta and/or a tool event."""

    text: str = ""
    tool_call: ToolCallPayload | None = None
    tool_result: ToolResultPayload | None = None
    handoff: HandoffPayload | None = None
    final: bool = False
    """Last chunk of the agent's turn."""
    ended: bool = False
    """The agent closed the conversation."""


class SessionSummary(FrozenModel):
    """What an adapter reports when a session closes."""

    turns: int = Field(default=0, ge=0)
    agent_session_id: str | None = None
    metadata: dict[str, str] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TimedFrame:
    """One received audio frame, stamped on arrival.

    A dataclass rather than a Pydantic model: 50 calls x 50 frames/s must not pay validation cost.
    """

    pcm: bytes
    """16-bit little-endian mono PCM."""
    sample_rate: int
    sample_index: int
    """Position of the first sample on the receive stream; the sample-accurate clock."""
    t_ms: int
    """Harness clock time when the frame arrived."""

    @property
    def duration_ms(self) -> float:
        """Frame length in milliseconds."""
        return len(self.pcm) / 2 / self.sample_rate * 1000


@runtime_checkable
class ChatSession(Protocol):
    """An open chat conversation with the agent."""

    def send(self, text: str) -> AsyncIterator[AgentChunk]:
        """Send a user message and stream the agent's reply until a ``final`` chunk."""
        ...

    async def close(self) -> SessionSummary:
        """End the conversation."""
        ...


@runtime_checkable
class ChatTarget(Protocol):
    """A chat agent that can open sessions."""

    async def open(self, ctx: SessionContext) -> ChatSession:
        """Open a new conversation for one trial."""
        ...


@runtime_checkable
class AudioLink(Protocol):
    """A live audio call with the agent."""

    async def send_frame(self, pcm: bytes, sample_rate: int) -> None:
        """Send one 20 ms frame of caller audio."""
        ...

    def frames(self) -> AsyncIterator[TimedFrame]:
        """Stream the agent's audio frames as they arrive."""
        ...

    async def hangup(self) -> None:
        """End the call."""
        ...


@runtime_checkable
class VoiceTarget(Protocol):
    """A voice agent that can be called."""

    async def connect(self, ctx: SessionContext) -> AudioLink:
        """Place a call for one trial."""
        ...


@runtime_checkable
class EmailTarget(Protocol):
    """An email agent."""

    async def deliver(self, msg: EmailMessage, ctx: SessionContext) -> None:
        """Deliver a customer email to the agent's inbox."""
        ...

    def replies(self, ctx: SessionContext) -> AsyncIterator[EmailMessage]:
        """Stream the agent's replies for this trial."""
        ...
