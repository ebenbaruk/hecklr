"""The email agent as an in-process callable for ``hecklr_adapters.email.PythonEmailTarget``.

Tools go to the trial's sandbox (``SessionContext.world_http_url``) when the session has one, else
to a private copy of the world; tests can pass their own ``backend`` factory (e.g. a
``TrialWorld`` without a server).
"""

from collections.abc import Callable
from types import TracebackType
from typing import Self

import httpx2
from acme_agent.tools import ToolBackend

from acme_email.desk import EmailDesk
from acme_email.tools import WorldRoute, backend_for
from hecklr_adapters import AgentSession
from hecklr_schemas import EmailMessage, World

SessionBackend = Callable[[AgentSession], ToolBackend]


class InProcessEmailAgent:
    """The email agent as a ``PythonEmailAgent``; closes its HTTP client on exit.

    Use as ``async with InProcessEmailAgent(desk, world) as agent``, then hand ``agent`` to
    ``PythonEmailTarget``.
    """

    def __init__(
        self, desk: EmailDesk, world: World, *, backend: SessionBackend | None = None
    ) -> None:
        self._desk = desk
        self._world = world
        self._backend = backend
        # No client timeout: tool calls run under the mailer's own deadline (DeadlineTools).
        self._client = httpx2.AsyncClient(timeout=None)

    def _default_backend(self, session: AgentSession) -> ToolBackend:
        url = session.context.world_http_url
        route = WorldRoute("header" if url else "local", url, session.context.trial_id)
        return backend_for(route, self._client, self._world)

    async def __call__(self, session: AgentSession, email: EmailMessage) -> EmailMessage:
        """Answer one customer email."""
        choose = self._backend or self._default_backend
        return await self._desk.answer(email, lambda: choose(session))

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
