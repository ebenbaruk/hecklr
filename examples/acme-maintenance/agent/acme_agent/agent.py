"""``AcmeAgent``: conversations, each with its own brain and tool backend."""

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final, Protocol

import anyio

from acme_agent.config import AgentConfigError, AgentFiles, AgentSettings
from acme_agent.llm_brain import LLMBrain
from acme_agent.rules.brain import RulesBrain
from acme_agent.spend import SpendMeter
from acme_agent.tools import Reply, ToolBackend, ToolRunner
from hecklr_schemas import LLMProvider

MAX_SESSIONS: Final = 10_000
"""Conversations kept in memory; the oldest is forgotten first, so a long server run is bounded."""

BackendFactory = Callable[[], ToolBackend]


class Brain(Protocol):
    """Decides what to say (and which tools to call) for one conversation."""

    async def reply(self, message: str, tools: ToolRunner) -> str:
        """Answer one customer message."""
        ...


@dataclass
class _Session:
    brain: Brain
    backend: ToolBackend
    lock: anyio.Lock = field(default_factory=anyio.Lock)


class AcmeAgent:
    """The reference agent: one brain per conversation, keyed by the caller's session id."""

    def __init__(
        self, files: AgentFiles, settings: AgentSettings, llm: LLMProvider | None = None
    ) -> None:
        """``llm`` is required for the LLM brain and unused by the rules brain."""
        self.files = files
        self.settings = settings
        self._new_brain: Callable[[], Brain]
        self._meter: SpendMeter | None = None
        if settings.brain == "rules":
            self._new_brain = lambda: RulesBrain(files, settings.today)
        elif llm is None:
            raise AgentConfigError("the llm brain needs an LLM provider")
        else:
            meter = SpendMeter(llm, settings.budget_usd)
            self._meter = meter
            self._new_brain = lambda: LLMBrain(files, settings, meter)
        self._sessions: OrderedDict[str, _Session] = OrderedDict()

    @property
    def spent_usd(self) -> Decimal:
        """What the agent's own model calls have cost so far (zero for the rules brain)."""
        return self._meter.spent_usd if self._meter is not None else Decimal(0)

    def _session(self, session_id: str, backend: BackendFactory) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            session = _Session(brain=self._new_brain(), backend=backend())
            self._sessions[session_id] = session
            if len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
        else:
            self._sessions.move_to_end(session_id)
        return session

    async def respond(self, session_id: str, message: str, backend: BackendFactory) -> Reply:
        """Answer one message of conversation ``session_id``.

        ``backend`` is called once, when the conversation starts; later messages reuse its tools
        (a local world keeps its state for the whole conversation). Messages of one conversation
        are answered one at a time.
        """
        session = self._session(session_id, backend)
        async with session.lock:
            runner = ToolRunner(session.backend)
            text = await session.brain.reply(message, runner)
        return Reply(
            text=text,
            steps=tuple(runner.steps),
            handoff=runner.handoff,
            reported_steps=session.backend.reports_calls,
        )
