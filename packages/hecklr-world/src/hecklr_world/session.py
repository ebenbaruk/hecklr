"""One trial's world: its store, its fault schedule and the events of every tool call."""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import anyio
import structlog
from pydantic import JsonValue

from hecklr_schemas.events import ErrorPayload, ToolCallPayload, ToolResultPayload
from hecklr_schemas.ids import new_id
from hecklr_schemas.trial import EventSink, TrialBrief
from hecklr_schemas.world import FaultKind, ToolDef, ToolFault, World, WorldSnapshot
from hecklr_world.errors import InternalToolError, ToolError, ToolTimeoutError
from hecklr_world.faults import FaultPlan, apply_stale_patch
from hecklr_world.store import Store
from hecklr_world.tools import Toolbox

_log = structlog.get_logger(__name__)


class SessionClosedError(RuntimeError):
    """A tool call reached a trial world that was already closed."""


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What one tool call returned to the agent."""

    call_id: str
    result: JsonValue
    error: ToolError | None
    fault: FaultKind | None

    @property
    def ok(self) -> bool:
        """Whether the call succeeded."""
        return self.error is None

    @property
    def http_status(self) -> int:
        """The status the OpenAPI server answers with."""
        return 200 if self.error is None else self.error.http_status

    def body(self) -> JsonValue:
        """The JSON the agent receives: the result, or ``{"error": {"code", "message"}}``."""
        return self.result if self.error is None else self.error.to_json()


@dataclass(frozen=True, slots=True)
class ObservedToolCall:
    """A tool call read from the agent's own OpenTelemetry span (observed mode)."""

    tool: str
    call_id: str
    args: dict[str, JsonValue]
    result: JsonValue
    error: str | None
    start_unix_ns: int
    end_unix_ns: int


@dataclass(slots=True)
class _HeldCall:
    tool: str
    start_ms: int


class TrialWorld:
    """A ``WorldSession``: one trial's isolated world.

    Use ``call_tool`` directly for in-process agents and tests; the shared ``Sandbox`` server
    routes HTTP, MCP and OTLP traffic for this trial here.
    """

    def __init__(
        self,
        brief: TrialBrief,
        world: World,
        sink: EventSink,
        *,
        observed: bool = False,
        mcp_url: str | None = None,
        http_url: str | None = None,
        on_close: Callable[["TrialWorld"], None] | None = None,
    ) -> None:
        self.trial_id: str = brief.trial_id
        self.world = world
        self.observed = observed
        self._sink = sink
        self._store = Store(world)
        self._toolbox = Toolbox(world)
        self._faults = FaultPlan(brief.scenario.faults, brief.seed, (t.name for t in world.tools))
        self._mcp_url = mcp_url
        self._http_url = http_url
        self._on_close = on_close
        self._closed = anyio.Event()
        self._held: dict[str, _HeldCall] = {}
        # Observed spans carry wall-clock (Unix) times; this pair maps them onto the trial clock.
        self._opened_unix_ns = time.time_ns()
        self._opened_ms = sink.now_ms()

    @property
    def mcp_url(self) -> str | None:
        """MCP endpoint of this trial's tools; ``None`` in observed mode or when not served."""
        return self._mcp_url

    @property
    def http_url(self) -> str | None:
        """OpenAPI base URL of this trial's tools (``{http_url}/openapi.json``)."""
        return self._http_url

    @property
    def tools(self) -> list[ToolDef]:
        """The world's tool definitions."""
        return self._toolbox.definitions

    @property
    def closed(self) -> bool:
        """Whether ``close()`` has run."""
        return self._closed.is_set()

    def snapshot(self) -> WorldSnapshot:
        """The full state now, stamped with the sink's clock."""
        return WorldSnapshot(
            world_id=self.world.id,
            taken_at_ms=self._sink.now_ms(),
            collections=self._store.state(),
        )

    async def close(self) -> None:
        """Release held (``timeout``) calls, recording them as timed out, and stop serving."""
        if self._closed.is_set():
            return
        self._closed.set()
        for call_id, held in list(self._held.items()):
            del self._held[call_id]
            error = ToolTimeoutError("no response before the trial ended")
            self._emit_result(call_id, held.tool, held.start_ms, None, error, FaultKind.TIMEOUT)
        if self._on_close is not None:
            self._on_close(self)

    async def call_tool(self, name: str, args: Mapping[str, JsonValue]) -> ToolOutcome:
        """Run one tool call as the agent made it, recording ``tool_call`` and ``tool_result``."""
        if self.closed:
            raise SessionClosedError(f"trial {self.trial_id} world is closed")
        call_id = new_id()
        start_ms = self._sink.now_ms()
        self._sink.emit("agent", ToolCallPayload(call_id=call_id, tool=name, args=dict(args)))
        fault = self._faults.next_fault(name) if name in self._toolbox else None
        kind = fault.kind if fault is not None else None
        try:
            result = await self._execute(call_id, name, args, fault, start_ms)
        except ToolTimeoutError as exc:
            # close() already recorded the held call's result.
            return ToolOutcome(call_id, None, exc, kind)
        except ToolError as exc:
            self._emit_result(call_id, name, start_ms, None, exc, kind)
            return ToolOutcome(call_id, None, exc, kind)
        except anyio.get_cancelled_exc_class():
            # The caller went away (agent-side timeout, server shutdown): still close the record.
            held = self._held.pop(call_id, None)
            recorded_by_close = kind is FaultKind.TIMEOUT and held is None
            if not recorded_by_close:
                error = ToolTimeoutError("the call was abandoned before it completed")
                self._emit_result(call_id, name, start_ms, None, error, kind)
            raise
        self._emit_result(call_id, name, start_ms, result, None, kind)
        return ToolOutcome(call_id, result, None, kind)

    async def _execute(
        self,
        call_id: str,
        name: str,
        args: Mapping[str, JsonValue],
        fault: ToolFault | None,
        start_ms: int,
    ) -> JsonValue:
        kind = fault.kind if fault is not None else None
        if kind is FaultKind.HTTP_500:
            raise InternalToolError("Internal Server Error")
        if kind is FaultKind.TIMEOUT:
            self._held[call_id] = _HeldCall(name, start_ms)
            await self._closed.wait()
            raise ToolTimeoutError("no response before the trial ended")
        if fault is not None and fault.delay_ms is not None:
            await anyio.sleep(fault.delay_ms / 1000)
        result = self._run(name, args)
        if fault is not None and fault.patch is not None:
            return apply_stale_patch(result, fault.patch)
        return result

    def _run(self, name: str, args: Mapping[str, JsonValue]) -> JsonValue:
        try:
            return self._toolbox.execute(self._store, name, args)
        except ToolError:
            raise
        except Exception as exc:
            # A crashing tool is a backend 500 to the agent; it is logged and recorded below.
            message = f"tool {name!r} crashed: {type(exc).__name__}: {exc}"
            _log.exception("world_tool_crashed", trial_id=self.trial_id, tool=name)
            self._sink.emit("tool", ErrorPayload(source="world", message=message))
            raise InternalToolError("Internal Server Error") from exc

    def _emit_result(
        self,
        call_id: str,
        tool: str,
        start_ms: int,
        result: JsonValue,
        error: ToolError | None,
        fault: FaultKind | None,
    ) -> None:
        latency_ms = self._sink.now_ms() - start_ms
        payload = ToolResultPayload(
            call_id=call_id,
            tool=tool,
            result=result,
            error=None if error is None else f"{error.code}: {error.message}",
            latency_ms=max(latency_ms, 0),
            fault=fault,
        )
        self._sink.emit("tool", payload)

    def record_observed(self, call: ObservedToolCall) -> None:
        """Record a tool call the agent made against its own backend (observed mode)."""
        if self.closed:
            raise SessionClosedError(f"trial {self.trial_id} world is closed")
        start_ms = self._trial_ms(call.start_unix_ns)
        end_ms = max(self._trial_ms(call.end_unix_ns), start_ms)
        self._sink.emit(
            "agent",
            ToolCallPayload(
                call_id=call.call_id, tool=call.tool, args=call.args, source="observed"
            ),
            t_ms=start_ms,
        )
        payload = ToolResultPayload(
            call_id=call.call_id,
            tool=call.tool,
            result=None if call.error is not None else call.result,
            error=call.error,
            latency_ms=end_ms - start_ms,
        )
        self._sink.emit("tool", payload, t_ms=end_ms)

    def _trial_ms(self, unix_ns: int) -> int:
        trial_ms = self._opened_ms + (unix_ns - self._opened_unix_ns) // 1_000_000
        return min(max(trial_ms, 0), self._sink.now_ms())
