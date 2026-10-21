"""One trial's world: its store, its fault schedule and the events of every tool call."""

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

import anyio
import structlog
from pydantic import JsonValue, TypeAdapter, ValidationError

from hecklr_schemas.events import ErrorPayload, ToolCallPayload, ToolResultPayload
from hecklr_schemas.ids import new_id
from hecklr_schemas.trial import EventSink, TrialBrief
from hecklr_schemas.world import FaultKind, ToolDef, ToolFault, World, WorldSnapshot
from hecklr_world.errors import InternalToolError, ToolError, ToolTimeoutError
from hecklr_world.faults import FaultPlan, apply_stale_patch
from hecklr_world.store import Store
from hecklr_world.tools import Toolbox

_log = structlog.get_logger(__name__)
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


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
    """A call waiting on a ``timeout`` or ``slow`` fault; ``close()`` releases it."""

    tool: str
    start_ms: int
    fault: FaultKind
    released: bool = False


class _ReleasedByCloseError(Exception):
    """A held call that ``close()`` already recorded as timed out."""

    def __init__(self, error: ToolTimeoutError) -> None:
        super().__init__(error.message)
        self.error = error


_RELEASED_MESSAGE = "no response before the trial ended"


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
        # OTLP exporters retry failed exports, so the same span can arrive more than once.
        self._observed_call_ids: set[str] = set()
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
        """Stop serving and release held calls, recording each as timed out.

        A call held by a ``timeout`` fault, or still sleeping through a ``slow`` one, gets its
        ``tool_result`` (error ``timeout``) here, before the caller takes the "after" snapshot, and
        never touches the store. The ``WorldSession`` Protocol calls these calls "dropped"; they
        are recorded rather than silently lost so every ``tool_call`` has its ``tool_result``.
        """
        if self._closed.is_set():
            return
        self._closed.set()
        for call_id, held in self._held.items():
            held.released = True
            error = ToolTimeoutError(_RELEASED_MESSAGE)
            self._emit_result(call_id, held.tool, held.start_ms, None, error, held.fault)
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
        except _ReleasedByCloseError as released:
            return ToolOutcome(call_id, None, released.error, kind)
        except ToolError as exc:
            self._emit_result(call_id, name, start_ms, None, exc, kind)
            return ToolOutcome(call_id, None, exc, kind)
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
        if fault is not None:
            if fault.kind is FaultKind.HTTP_500:
                raise InternalToolError("Internal Server Error")
            if fault.kind is FaultKind.TIMEOUT:
                await self._hold(call_id, name, start_ms, fault.kind, None)
            elif fault.delay_ms is not None:
                await self._hold(call_id, name, start_ms, fault.kind, fault.delay_ms / 1000)
        result = self._run(name, args)
        if fault is not None and fault.patch is not None:
            return apply_stale_patch(result, fault.patch)
        return result

    async def _hold(
        self, call_id: str, tool: str, start_ms: int, fault: FaultKind, delay_s: float | None
    ) -> None:
        """Wait ``delay_s`` (forever when ``None``) unless the trial ends first.

        This is the only await of a tool call, so cancellation (agent-side timeout, server
        shutdown) is recorded here too.
        """
        held = _HeldCall(tool, start_ms, fault)
        self._held[call_id] = held
        try:
            with anyio.move_on_after(math.inf if delay_s is None else delay_s):
                await self._closed.wait()
        except anyio.get_cancelled_exc_class():
            if not held.released:
                error = ToolTimeoutError("the call was abandoned before it completed")
                self._emit_result(call_id, tool, start_ms, None, error, fault)
            raise
        finally:
            del self._held[call_id]
        if held.released:
            raise _ReleasedByCloseError(ToolTimeoutError(_RELEASED_MESSAGE))

    def _run(self, name: str, args: Mapping[str, JsonValue]) -> JsonValue:
        try:
            result = self._toolbox.execute(self._store, name, args)
        except ToolError:
            raise
        except Exception as exc:
            self._crashed(name, f"{type(exc).__name__}: {exc}")
            raise InternalToolError("Internal Server Error") from exc
        try:
            # Python tools may return anything; only JSON can reach the agent and the event log.
            return _JSON.validate_python(result, strict=True)
        except ValidationError as exc:
            self._crashed(name, f"returned a value that is not JSON: {exc.errors()[0]['msg']}")
            raise InternalToolError("Internal Server Error") from exc

    def _crashed(self, tool: str, reason: str) -> None:
        # A broken tool is a backend 500 to the agent; the harness sees why in the log and events.
        message = f"tool {tool!r} crashed: {reason}"
        _log.exception("world_tool_crashed", trial_id=self.trial_id, tool=tool, reason=reason)
        self._sink.emit("tool", ErrorPayload(source="world", message=message))

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
        """Record a tool call the agent made against its own backend (observed mode).

        A ``call_id`` already recorded (an exporter retrying the same span) is ignored.
        """
        if self.closed:
            raise SessionClosedError(f"trial {self.trial_id} world is closed")
        if call.call_id in self._observed_call_ids:
            return
        self._observed_call_ids.add(call.call_id)
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
