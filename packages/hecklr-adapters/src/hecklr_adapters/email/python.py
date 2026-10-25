"""The in-process email adapter: an email agent that is an async callable in this process.

The callable gets the conversation's session and one customer email, and returns its replies::

    async def agent(session: AgentSession, msg: EmailMessage) -> EmailMessage | list[...] | None

``hecklr_adapters.email.make_reply`` builds a correctly threaded reply. Anything the callable
raises is the agent's failure (``AgentError``). Replies are queued per trial and streamed by
``replies``, in the order the agent returned them.
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import anyio

from hecklr_adapters.email.messages import storable
from hecklr_adapters.errors import AgentError
from hecklr_adapters.python import AgentSession
from hecklr_schemas import EmailMessage, SessionContext, new_id

PythonEmailAgent = Callable[
    [AgentSession, EmailMessage], Awaitable[EmailMessage | Sequence[EmailMessage] | None]
]
"""The shape of an in-process email agent: one call per customer email."""


@dataclass
class _Trial:
    sessions: dict[str, AgentSession] = field(default_factory=dict)
    """One session per customer address."""
    pending: list[EmailMessage] = field(default_factory=list)
    arrived: anyio.Event = field(default_factory=anyio.Event)
    """Set when ``pending`` gains replies; replaced once the waiter has woken."""


class PythonEmailTarget:
    """Delivers customer emails to an in-process agent and streams back what it answers.

    A session (and its id) is per customer address, so a retried trial, which writes from a fresh
    address, never meets the memory of a failed attempt. A trial's state is dropped when its
    ``replies`` stream is closed.
    """

    def __init__(self, agent: PythonEmailAgent) -> None:
        self._agent = agent
        self._trials: dict[str, _Trial] = {}

    async def deliver(self, msg: EmailMessage, ctx: SessionContext) -> None:
        """Hand ``msg`` to the agent and queue its replies for ``replies``."""
        trial = self._trials.setdefault(ctx.trial_id, _Trial())
        session = trial.sessions.setdefault(msg.from_addr, AgentSession(id=new_id(), context=ctx))
        try:
            answer = await self._agent(session, msg)
        except AgentError:
            raise
        except Exception as exc:
            raise AgentError(f"agent raised {type(exc).__name__}: {exc}") from exc
        replies = [storable(reply) for reply in _as_replies(answer)]
        trial.pending.extend(replies)
        trial.arrived.set()

    async def replies(self, ctx: SessionContext) -> AsyncIterator[EmailMessage]:
        """Stream this trial's replies as the agent produces them; never ends by itself."""
        trial = self._trials.setdefault(ctx.trial_id, _Trial())
        try:
            while True:
                while trial.pending:
                    yield trial.pending.pop(0)
                await trial.arrived.wait()
                trial.arrived = anyio.Event()
        finally:
            self._trials.pop(ctx.trial_id, None)


def _as_replies(answer: object) -> list[EmailMessage]:
    if answer is None:
        return []
    if isinstance(answer, EmailMessage):
        return [answer]
    if isinstance(answer, Sequence) and all(isinstance(m, EmailMessage) for m in answer):
        return list(answer)
    raise AgentError(f"agent produced {type(answer).__name__}; expected EmailMessage(s) or None")
