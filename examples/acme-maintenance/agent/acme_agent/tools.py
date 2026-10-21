"""Where the agent's tools run, and what one agent turn produced.

Two backends:

- ``HttpWorldTools``: the trial's Hecklr world sandbox over its OpenAPI routes
  (``POST {world_url}/tools/{name}``). The sandbox records every call as events itself, so the
  agent must not report them too (ADR 0006a: they would be logged twice).
- ``LocalWorldTools``: an in-process copy of the world, for running the agent without Hecklr
  (manual testing, demos). Nobody else sees these calls, so the agent reports them in its stream.

Creating an emergency ticket transfers the customer to a human: ``ToolRunner`` turns a successful
``create_emergency_ticket`` into a handoff, whichever brain made the call.
"""

import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol

import httpx2
from pydantic import JsonValue

from hecklr_schemas import (
    TRIAL_ID_HEADER,
    AgentChunk,
    HandoffPayload,
    ToolCallPayload,
    ToolResultPayload,
    World,
    new_id,
)
from hecklr_schemas.world import Record
from hecklr_world import Store, ToolError
from hecklr_world.tools import Toolbox

HANDOFF_TOOL: Final = "create_emergency_ticket"
HANDOFF_TARGET: Final = "on-call engineer"
_WORD = re.compile(r"\S+\s*")


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What a tool call returned: the result, or an ``{"error": {...}}`` body."""

    ok: bool
    body: JsonValue

    def error_text(self) -> str:
        """``code: message`` of a failed call."""
        error = self.body.get("error") if isinstance(self.body, dict) else None
        if isinstance(error, dict):
            return f"{error.get('code', 'error')}: {error.get('message', '')}"
        return str(self.body)


class ToolBackend(Protocol):
    """Runs the agent's tool calls somewhere."""

    @property
    def reports_calls(self) -> bool:
        """Whether the agent must report its calls (Hecklr cannot see this backend)."""
        ...

    async def call(self, name: str, args: Mapping[str, JsonValue]) -> ToolOutcome:
        """Run one call; backend failures are returned as error outcomes, never raised."""
        ...


def _error(code: str, message: str) -> ToolOutcome:
    return ToolOutcome(ok=False, body={"error": {"code": code, "message": message}})


class HttpWorldTools:
    """A trial's world sandbox, reached over HTTP."""

    reports_calls = False

    def __init__(self, client: httpx2.AsyncClient, world_url: str, trial_id: str | None) -> None:
        self._client = client
        self._url = world_url.rstrip("/")
        self._headers = {TRIAL_ID_HEADER: trial_id} if trial_id else {}

    async def call(self, name: str, args: Mapping[str, JsonValue]) -> ToolOutcome:
        """POST the arguments; a timeout or an unreachable backend is an error outcome."""
        try:
            response = await self._client.post(
                f"{self._url}/tools/{name}", json=dict(args), headers=self._headers
            )
        except httpx2.TimeoutException:
            return _error("timeout", f"{name} did not answer in time")
        except httpx2.HTTPError as exc:
            return _error("unavailable", f"{name} failed: {type(exc).__name__}")
        try:
            body: JsonValue = response.json()
        except ValueError:
            return _error("bad_response", f"{name} returned HTTP {response.status_code}")
        return ToolOutcome(ok=response.status_code == httpx2.codes.OK, body=body)


class LocalWorldTools:
    """A private, in-process copy of the world for one conversation."""

    reports_calls = True

    def __init__(self, world: World) -> None:
        self._store = Store(world)
        self._toolbox = Toolbox(world)

    async def call(self, name: str, args: Mapping[str, JsonValue]) -> ToolOutcome:
        """Run the declarative tool on this conversation's store."""
        try:
            return ToolOutcome(ok=True, body=self._toolbox.execute(self._store, name, args))
        except ToolError as exc:
            return ToolOutcome(ok=False, body=exc.to_json())

    def state(self) -> dict[str, dict[str, Record]]:
        """The conversation's world state now: collection -> record key -> record."""
        return self._store.state()


@dataclass(frozen=True, slots=True)
class ToolStep:
    """One tool call of a turn and its result, in the ``hecklr_schemas`` shapes."""

    call: ToolCallPayload
    result: ToolResultPayload


class ToolRunner:
    """The brains' only way to call tools: records each call and detects handoffs."""

    def __init__(self, backend: ToolBackend) -> None:
        self._backend = backend
        self.steps: list[ToolStep] = []
        self.handoff: HandoffPayload | None = None

    async def call(self, name: str, args: Mapping[str, JsonValue]) -> ToolOutcome:
        """Run one tool call."""
        call_id = new_id()
        started = time.monotonic_ns()
        outcome = await self._backend.call(name, args)
        latency_ms = (time.monotonic_ns() - started) // 1_000_000
        self.steps.append(
            ToolStep(
                call=ToolCallPayload(call_id=call_id, tool=name, args=dict(args)),
                result=ToolResultPayload(
                    call_id=call_id,
                    tool=name,
                    result=outcome.body if outcome.ok else None,
                    error=None if outcome.ok else outcome.error_text(),
                    latency_ms=latency_ms,
                ),
            )
        )
        if outcome.ok and name == HANDOFF_TOOL:
            category = args.get("category")
            self.handoff = HandoffPayload(
                reason=f"emergency: {category}" if isinstance(category, str) else "emergency",
                target=HANDOFF_TARGET,
            )
        return outcome


@dataclass(frozen=True, slots=True)
class Reply:
    """Everything one agent turn produced."""

    text: str
    steps: tuple[ToolStep, ...] = ()
    handoff: HandoffPayload | None = None
    reported_steps: bool = False
    """Whether the stream carries the tool steps (only when Hecklr cannot see the backend)."""

    @property
    def ended(self) -> bool:
        """A handoff ends the agent's part of the conversation."""
        return self.handoff is not None

    def chunks(self) -> list[AgentChunk]:
        """The turn as a stream: tool events (if reported), text word by word, handoff, end."""
        chunks: list[AgentChunk] = []
        if self.reported_steps:
            for step in self.steps:
                chunks.append(AgentChunk(tool_call=step.call))
                chunks.append(AgentChunk(tool_result=step.result))
        chunks.extend(AgentChunk(text=word) for word in _WORD.findall(self.text))
        if self.handoff is not None:
            chunks.append(AgentChunk(handoff=self.handoff))
            chunks.append(AgentChunk(ended=True))
        return chunks
