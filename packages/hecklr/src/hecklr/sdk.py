"""In-process agents: ``@hecklr.agent`` and the ``hecklr.Session`` they receive.

A decorated function is referenced from ``hecklr.yaml`` like any Python adapter::

    agent:
      adapters:
        chat: { type: python, entrypoint: "my_bot.agent:reply" }

and is called once per user message with the conversation's ``Session``::

    @hecklr.agent(channel="chat")
    async def reply(session: hecklr.Session, message: str) -> str:
        return await bot.answer(session.id, message)

A streaming agent is an async generator yielding text deltas (or ``AgentChunk`` values, to report
tool calls, handoffs or the end of the conversation).
"""

import functools
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal

from hecklr_adapters import AgentSession
from hecklr_schemas import TRIAL_ID_HEADER, AgentChunk, SessionContext

AgentReply = Awaitable[str] | AsyncIterator[str | AgentChunk]
"""What an in-process agent returns for one message: the whole reply, or a stream of it."""

AgentFunction = Callable[["Session", str], AgentReply]
"""The signature ``@hecklr.agent`` accepts."""

IN_PROCESS_CHANNELS: Final = ("chat",)
"""Channels an in-process agent can serve; voice and email agents are reached over the network."""


@dataclass(frozen=True, slots=True)
class Session:
    """One conversation between a simulated customer and an in-process agent."""

    id: str
    """Stable for the whole conversation and fresh for every trial attempt; key memory on it."""
    trial_id: str
    """The trial this conversation belongs to (also sent as the ``X-Hecklr-Trial-Id`` header)."""
    seed: int
    """The trial seed, for agents that want reproducible randomness."""
    world_mcp_url: str | None
    """MCP endpoint of this trial's world sandbox; point the agent's tools here."""
    world_http_url: str | None
    """OpenAPI endpoint of this trial's world sandbox (``POST {url}/tools/<name>``)."""

    @property
    def headers(self) -> dict[str, str]:
        """Headers every request the agent makes on this trial's behalf should carry."""
        return {TRIAL_ID_HEADER: self.trial_id}

    @classmethod
    def from_context(cls, session_id: str, ctx: SessionContext) -> "Session":
        """The session for adapter session ``session_id`` of the trial in ``ctx``."""
        return cls(
            id=session_id,
            trial_id=ctx.trial_id,
            seed=ctx.seed,
            world_mcp_url=ctx.world_mcp_url,
            world_http_url=ctx.world_http_url,
        )


def agent(
    *, channel: Literal["chat"] = "chat"
) -> Callable[[AgentFunction], Callable[[AgentSession | Session, str], AgentReply]]:
    """Mark an async function (or async generator) as an in-process Hecklr agent.

    The returned function is what the ``python`` adapter calls: it receives the adapter's session,
    hands the agent a ``hecklr.Session``, and returns the agent's reply unchanged. It also accepts
    a ``Session`` directly, so the agent stays callable from the user's own tests.

    Raises:
        ValueError: ``channel`` cannot be served in process.
        TypeError: the decorated object is not an async function or async generator function.
    """
    if channel not in IN_PROCESS_CHANNELS:
        raise ValueError(
            f"in-process agents serve {list(IN_PROCESS_CHANNELS)} only, not {channel!r}; "
            "reach voice and email agents through their network adapters"
        )

    def decorate(function: AgentFunction) -> Callable[[AgentSession | Session, str], AgentReply]:
        if not (inspect.iscoroutinefunction(function) or inspect.isasyncgenfunction(function)):
            raise TypeError(
                f"@hecklr.agent needs an async function or async generator, got {function!r}"
            )

        @functools.wraps(function)
        def call(session: AgentSession | Session, message: str) -> AgentReply:
            if isinstance(session, AgentSession):
                session = Session.from_context(session.id, session.context)
            return function(session, message)

        return call

    return decorate
