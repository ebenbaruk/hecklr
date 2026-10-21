"""The ``python`` chat adapter: an agent that is an async callable in this process.

This is what ``@hecklr.agent`` wraps. The callable takes the session and the user message and
returns either the whole reply or a stream of it::

    async def agent(session: AgentSession, message: str) -> str: ...
    async def agent(session: AgentSession, message: str) -> AsyncIterator[str | AgentChunk]: ...

Streaming ``AgentChunk`` lets an in-process agent report tool calls, handoffs and the end of the
conversation. Anything the callable raises is the agent's failure (``AgentError``).
"""

import importlib
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import cast

from hecklr_adapters.errors import AdapterConfigError, AgentError
from hecklr_schemas import AgentChunk, SessionContext, SessionSummary, new_id


@dataclass(frozen=True, slots=True)
class AgentSession:
    """What an in-process agent sees of its conversation."""

    id: str
    """Stable for the whole conversation; key the agent's memory on it."""
    context: SessionContext
    """Trial id, seed and the world sandbox endpoints the agent's tools should call."""


PythonAgent = Callable[[AgentSession, str], Awaitable[str] | AsyncIterator[str | AgentChunk]]
"""The shape of an in-process agent: one call per user message."""


def load_agent(entrypoint: str) -> PythonAgent:
    """Import ``module.path:function`` and return the agent callable."""
    module_name, _, attribute = entrypoint.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise AdapterConfigError(f"cannot import agent module {module_name!r}: {exc}") from exc
    agent = getattr(module, attribute, None)
    if not callable(agent):
        raise AdapterConfigError(f"{entrypoint!r} is not a callable agent")
    # Only the call shape can be checked at runtime; the documented signature is the contract.
    return cast(PythonAgent, agent)


class PythonChatTarget:
    """Opens sessions with an in-process agent callable."""

    def __init__(self, agent: PythonAgent) -> None:
        self._agent = agent

    async def open(self, ctx: SessionContext) -> "PythonChatSession":
        """Start a conversation with a fresh session id."""
        return PythonChatSession(self._agent, AgentSession(id=new_id(), context=ctx))


class PythonChatSession:
    """One conversation with an in-process agent."""

    def __init__(self, agent: PythonAgent, session: AgentSession) -> None:
        self._agent = agent
        self._session = session
        self._turns = 0

    async def send(self, text: str) -> AsyncIterator[AgentChunk]:
        """Call the agent and stream its reply, ending with exactly one ``final`` chunk."""
        try:
            reply = self._agent(self._session, text)
            if isinstance(reply, AsyncIterator):
                async for item in reply:
                    chunk = _as_chunk(item).model_copy(update={"final": False})
                    if chunk != AgentChunk():
                        yield chunk
            else:
                yield _as_chunk(await reply)
        except AgentError:
            raise
        except Exception as exc:
            raise AgentError(f"agent raised {type(exc).__name__}: {exc}") from exc
        self._turns += 1
        yield AgentChunk(final=True)

    async def close(self) -> SessionSummary:
        """Nothing to release in process; report the session."""
        return SessionSummary(turns=self._turns, agent_session_id=self._session.id)


def _as_chunk(item: object) -> AgentChunk:
    if isinstance(item, AgentChunk):
        return item
    if isinstance(item, str):
        return AgentChunk(text=item)
    raise AgentError(f"agent produced {type(item).__name__}; expected str or AgentChunk")
