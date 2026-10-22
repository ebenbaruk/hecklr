"""Langfuse exports (traces or observations, JSON or JSONL) to conversations.

Langfuse's UI exports trace rows and its public API returns trace or observation rows (v2
observations return ``input``/``output`` as raw strings). A conversation is a Langfuse session
(``sessionId``); a trace without a session is its own conversation. Each root row (a trace, or an
observation without a parent) is one agent invocation; ``TOOL`` observations are tool calls of the
invocation in the same trace.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from hecklr_mine.steps import (
    Record,
    Step,
    as_object,
    assemble,
    first_string,
    last_text,
    maybe_json,
    messages_in,
    parse_time,
    read_records,
    tool_args,
)
from hecklr_schemas import Conversation, ConversationToolCall

INPUT_KEYS: Final = ("input", "question", "query", "message", "text", "content")
OUTPUT_KEYS: Final = ("output", "answer", "response", "text", "content", "completion")
_ROOT_TYPES: Final = frozenset({"SPAN", "AGENT", "CHAIN", "GENERATION"})


def load_langfuse(path: Path) -> list[Conversation]:
    """Read a Langfuse trace or observation export.

    Raises:
        IngestError: The file is not JSON, or a conversation has no messages.
    """
    records = read_records(path)
    tools: dict[str, list[ConversationToolCall]] = {}
    roots: list[tuple[int, Record]] = []
    for order, record in enumerate(records):
        kind = record.get("type")
        if kind == "TOOL":
            tools.setdefault(str(record.get("traceId")), []).append(_tool_call(record))
        elif _is_root(record, kind):
            roots.append((order, record))
    steps = [_step(order, record, tools) for order, record in roots]
    return assemble(steps, source="langfuse")


def _is_root(record: Mapping[str, JsonValue], kind: JsonValue) -> bool:
    if kind is None:
        return True  # trace rows have no observation type
    if record.get("isRootObservation") is True:
        return True
    return kind in _ROOT_TYPES and not record.get("parentObservationId")


def _trace_id(record: Mapping[str, JsonValue]) -> str:
    return str(record.get("traceId") or record.get("id"))


def _step(
    order: int, record: Mapping[str, JsonValue], tools: Mapping[str, list[ConversationToolCall]]
) -> Step:
    trace_id = _trace_id(record)
    inputs, outputs = record.get("input"), record.get("output")
    session = record.get("sessionId")
    return Step(
        conversation_id=str(session) if session else trace_id,
        order=order,
        start=parse_time(record.get("timestamp") or record.get("startTime")),
        user=last_text(messages_in(inputs), "user") or first_string(inputs, INPUT_KEYS),
        agent=last_text(messages_in(outputs), "agent") or first_string(outputs, OUTPUT_KEYS),
        tool_calls=list(tools.get(trace_id, [])),
        metadata=as_object(record.get("metadata")),
    )


def _tool_call(record: Mapping[str, JsonValue]) -> ConversationToolCall:
    error = record.get("statusMessage") if record.get("level") == "ERROR" else None
    return ConversationToolCall(
        tool=str(record.get("name") or "unknown_tool"),
        args=tool_args(record.get("input")),
        result=maybe_json(record.get("output")),
        error=str(error) if error else None,
    )
