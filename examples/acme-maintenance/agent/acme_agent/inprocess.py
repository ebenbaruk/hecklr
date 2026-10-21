"""The agent as an in-process callable for Hecklr's ``python`` chat adapter.

``InProcessAgent`` has the ``hecklr_adapters.PythonAgent`` shape: called once per customer
message with the adapter's ``AgentSession``, it streams ``AgentChunk``s. Tools go to the trial's
sandbox (``SessionContext.world_http_url``) when the session has one, else to a private copy of
the world; tests can pass their own ``backend`` factory (e.g. a ``TrialWorld`` without a server).
"""

from collections.abc import AsyncIterator, Callable
from types import TracebackType
from typing import Self

import httpx2

from acme_agent.agent import AcmeAgent
from acme_agent.tools import HttpWorldTools, LocalWorldTools, ToolBackend
from hecklr_adapters import AgentSession
from hecklr_schemas import AgentChunk, World

SessionBackend = Callable[[AgentSession], ToolBackend]


class InProcessAgent:
    """``async with InProcessAgent(agent, world) as chat:`` then hand ``chat`` to the adapter."""

    def __init__(
        self, agent: AcmeAgent, world: World, *, backend: SessionBackend | None = None
    ) -> None:
        self._agent = agent
        self._world = world
        self._backend = backend
        self._client = httpx2.AsyncClient(timeout=agent.settings.tool_timeout_s)

    def _default_backend(self, session: AgentSession) -> ToolBackend:
        url = session.context.world_http_url
        if url is not None:
            return HttpWorldTools(self._client, url, session.context.trial_id)
        return LocalWorldTools(self._world)

    async def __call__(self, session: AgentSession, message: str) -> AsyncIterator[AgentChunk]:
        """Answer one message as a stream of chunks."""
        choose = self._backend or self._default_backend
        reply = await self._agent.respond(session.id, message, lambda: choose(session))
        for chunk in reply.chunks():
            yield chunk

    async def aclose(self) -> None:
        """Release the HTTP client used for sandbox tools."""
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()
