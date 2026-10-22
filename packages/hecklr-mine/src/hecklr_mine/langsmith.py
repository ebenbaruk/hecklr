"""LangSmith run exports (JSON or JSONL of runs, e.g. from ``Client.list_runs``) to conversations.

LangSmith groups the traces of one conversation into a thread through a metadata key on the runs
(``thread_id``, ``session_id`` or ``conversation_id``); a trace without one is its own
conversation. Each root run (no ``parent_run_id``) is one agent invocation; runs of type ``tool``
are tool calls of the invocation in the same trace.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from hecklr_mine.langfuse import INPUT_KEYS, OUTPUT_KEYS
from hecklr_mine.steps import (
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

THREAD_KEYS: Final = ("thread_id", "session_id", "conversation_id")
"""Run metadata keys LangSmith (and common integrations) use to group a thread."""


def load_langsmith(path: Path) -> list[Conversation]:
    """Read a LangSmith run export.

    Raises:
        IngestError: The file is not JSON, or a conversation has no messages.
    """
    records = read_records(path)
    tools: dict[str, list[ConversationToolCall]] = {}
    for record in records:
        if record.get("run_type") == "tool":
            tools.setdefault(_trace_id(record), []).append(_tool_call(record))
    steps = [
        _step(order, record, tools)
        for order, record in enumerate(records)
        if not record.get("parent_run_id") and record.get("run_type") != "tool"
    ]
    return assemble(steps, source="langsmith")


def _trace_id(record: Mapping[str, JsonValue]) -> str:
    return str(record.get("trace_id") or record.get("id"))


def _metadata(record: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    extra = as_object(record.get("extra"))
    return as_object(extra.get("metadata")) or as_object(record.get("metadata"))


def _step(
    order: int, record: Mapping[str, JsonValue], tools: Mapping[str, list[ConversationToolCall]]
) -> Step:
    trace_id = _trace_id(record)
    metadata = _metadata(record)
    thread = next((metadata[key] for key in THREAD_KEYS if metadata.get(key)), None)
    inputs, outputs = record.get("inputs"), record.get("outputs")
    return Step(
        conversation_id=str(thread) if thread is not None else trace_id,
        order=order,
        start=parse_time(record.get("start_time")),
        user=last_text(messages_in(inputs), "user") or first_string(inputs, INPUT_KEYS),
        agent=last_text(messages_in(outputs), "agent") or first_string(outputs, OUTPUT_KEYS),
        tool_calls=list(tools.get(trace_id, [])),
        metadata={k: v for k, v in metadata.items() if k not in THREAD_KEYS},
    )


def _tool_call(record: Mapping[str, JsonValue]) -> ConversationToolCall:
    inputs = as_object(record.get("inputs"))
    outputs = maybe_json(record.get("outputs"))
    result = outputs.get("output", outputs) if isinstance(outputs, dict) else outputs
    error = record.get("error")
    return ConversationToolCall(
        tool=str(record.get("name") or "unknown_tool"),
        # LangChain tools log their arguments as a JSON string under ``input``.
        args=tool_args(inputs["input"]) if set(inputs) == {"input"} else inputs,
        result=maybe_json(result),
        error=str(error) if error else None,
    )
