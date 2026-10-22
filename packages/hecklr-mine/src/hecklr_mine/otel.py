"""OpenTelemetry GenAI spans (OTLP JSON) to conversations.

Reads what an OTLP/JSON exporter or the collector's file exporter writes (``resourceSpans``
documents, one per line or as one document), using the GenAI semantic conventions:
``gen_ai.operation.name``, ``gen_ai.conversation.id``, ``gen_ai.input.messages`` and
``gen_ai.output.messages`` (``{"role", "parts"}`` messages), and ``execute_tool`` spans with
``gen_ai.tool.name``, ``gen_ai.tool.call.arguments`` and ``gen_ai.tool.call.result``.

An agent invocation usually nests model calls (``invoke_agent`` around several ``chat`` spans), so
only the outermost message-bearing span of each subtree becomes a step; tool spans inside it are
that step's tool calls.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from hecklr_mine.errors import IngestError
from hecklr_mine.steps import (
    Record,
    Step,
    assemble,
    last_text,
    maybe_json,
    messages_in,
    parse_time,
    read_records,
    tool_args,
)
from hecklr_schemas import Conversation, ConversationToolCall

MESSAGE_OPERATIONS: Final = frozenset(
    {"chat", "text_completion", "generate_content", "invoke_agent"}
)
"""Operations whose spans carry the conversation's messages."""
TOOL_OPERATION: Final = "execute_tool"
_ANY_VALUE_KEYS: Final = ("stringValue", "boolValue", "intValue", "doubleValue", "bytesValue")


@dataclass(frozen=True, slots=True)
class Span:
    """The parts of an OTLP span the miner reads."""

    trace_id: str
    span_id: str
    parent_id: str | None
    start: str
    attributes: dict[str, JsonValue]
    error: str | None

    @property
    def operation(self) -> str | None:
        """``gen_ai.operation.name``, if set."""
        value = self.attributes.get("gen_ai.operation.name")
        return value if isinstance(value, str) else None


def load_otel(path: Path) -> list[Conversation]:
    """Read OTLP JSON trace exports and rebuild one conversation per ``gen_ai.conversation.id``.

    Spans without a conversation id are grouped by trace. Spans that are neither message-bearing
    nor tool executions (HTTP, database, ...) are ignored.

    Raises:
        IngestError: The file is not OTLP JSON or a conversation cannot be rebuilt.
    """
    spans = [span for record in read_records(path) for span in _spans(path, record)]
    return assemble(_steps(spans), source="otel")


def _spans(path: Path, record: Record) -> list[Span]:
    if "spanId" in record:
        return [_span(record)]
    resource_spans = record.get("resourceSpans")
    if not isinstance(resource_spans, list):
        raise IngestError(f"{path}: not OTLP JSON (no resourceSpans)")
    spans = []
    for resource in resource_spans:
        scope_spans = resource.get("scopeSpans", []) if isinstance(resource, dict) else []
        for scope in scope_spans if isinstance(scope_spans, list) else []:
            raw_spans = scope.get("spans", []) if isinstance(scope, dict) else []
            for raw in raw_spans if isinstance(raw_spans, list) else []:
                if isinstance(raw, dict):
                    spans.append(_span(raw))
    return spans


def _span(raw: Mapping[str, JsonValue]) -> Span:
    attributes = _attributes(raw.get("attributes"))
    status = raw.get("status")
    error = None
    if isinstance(status, dict) and status.get("code") in (2, "STATUS_CODE_ERROR"):
        error = str(status.get("message") or attributes.get("error.type") or "error")
    parent = raw.get("parentSpanId")
    return Span(
        trace_id=str(raw.get("traceId", "")),
        span_id=str(raw.get("spanId", "")),
        parent_id=str(parent) if parent else None,
        start=str(raw.get("startTimeUnixNano", "")),
        attributes=attributes,
        error=error,
    )


def _attributes(raw: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(raw, list):
        return {}
    decoded: dict[str, JsonValue] = {}
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("key"), str):
            decoded[str(item["key"])] = any_value(item.get("value"))
    return decoded


def any_value(value: JsonValue) -> JsonValue:
    """Decode an OTLP ``AnyValue`` (``{"stringValue": ...}``, arrays, key-value lists)."""
    if not isinstance(value, dict):
        return value
    for key in _ANY_VALUE_KEYS:
        if key in value:
            scalar = value[key]
            return int(scalar) if key == "intValue" and isinstance(scalar, str) else scalar
    array = value.get("arrayValue")
    if isinstance(array, dict):
        items = array.get("values", [])
        return [any_value(item) for item in items] if isinstance(items, list) else []
    kvlist = value.get("kvlistValue")
    if isinstance(kvlist, dict):
        return _attributes(kvlist.get("values"))
    return None


def _steps(spans: Sequence[Span]) -> list[Step]:
    by_id = {span.span_id: span for span in spans}
    conversation_of = _conversation_ids(spans)
    owners = {span.span_id: _message_owner(span, by_id) for span in spans}
    steps: dict[str, Step] = {}
    for order, span in enumerate(spans):
        if span.operation in MESSAGE_OPERATIONS and owners[span.span_id] == span.span_id:
            steps[span.span_id] = _message_step(span, conversation_of[span.trace_id], order)
    for order, span in enumerate(spans):
        if span.operation != TOOL_OPERATION:
            continue
        owner = owners[span.span_id]
        if owner is not None:
            steps[owner].tool_calls.append(_tool_call(span))
        else:
            # Tools run by application code beside the model call: their own step, attached to
            # the next agent reply by ``assemble``.
            steps[span.span_id] = Step(
                conversation_id=conversation_of[span.trace_id],
                order=order,
                start=parse_time(span.start),
                tool_calls=[_tool_call(span)],
            )
    return list(steps.values())


def _conversation_ids(spans: Sequence[Span]) -> dict[str, str]:
    """Trace id -> the trace's first ``gen_ai.conversation.id``, else the trace id itself."""
    ids: dict[str, str] = {}
    for span in spans:
        value = span.attributes.get("gen_ai.conversation.id")
        if isinstance(value, str) and value:
            ids.setdefault(span.trace_id, value)
    return {span.trace_id: ids.get(span.trace_id, span.trace_id) for span in spans}


def _message_owner(span: Span, by_id: Mapping[str, Span]) -> str | None:
    """The outermost message-bearing span at or above ``span``, if any."""
    owner = None
    current: Span | None = span
    seen: set[str] = set()
    while current is not None and current.span_id not in seen:
        seen.add(current.span_id)
        if current.operation in MESSAGE_OPERATIONS:
            owner = current.span_id
        current = by_id.get(current.parent_id) if current.parent_id else None
    return owner


def _message_step(span: Span, conversation_id: str, order: int) -> Step:
    inputs = messages_in(span.attributes.get("gen_ai.input.messages"))
    outputs = messages_in(span.attributes.get("gen_ai.output.messages"))
    return Step(
        conversation_id=conversation_id,
        order=order,
        start=parse_time(span.start),
        user=last_text(inputs, "user"),
        agent="\n".join(m.text for m in outputs if m.role == "agent" and m.text.strip()) or None,
    )


def _tool_call(span: Span) -> ConversationToolCall:
    name = span.attributes.get("gen_ai.tool.name")
    return ConversationToolCall(
        tool=str(name) if name else "unknown_tool",
        args=tool_args(span.attributes.get("gen_ai.tool.call.arguments")),
        result=maybe_json(span.attributes.get("gen_ai.tool.call.result")),
        error=span.error,
    )
