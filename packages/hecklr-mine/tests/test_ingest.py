"""Stage 1 (WS13 ingest): every supported export becomes the same frozen Conversation."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue

from hecklr_mine import CsvMapping, IngestError, LogFormat, load_logs
from hecklr_mine.otel import any_value
from hecklr_mine.steps import messages_in, parse_time, read_records, text_of
from hecklr_schemas import Channel, Conversation


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


def _dialogue(conversation: Conversation) -> list[tuple[str, str]]:
    return [(turn.role, turn.text) for turn in conversation.turns]


# --- native JSONL --------------------------------------------------------------------------


def test_ws13_jsonl_round_trips_native_conversations(tmp_path: Path) -> None:
    conversation = Conversation.model_validate(
        {
            "id": "c-1",
            "turns": [
                {"role": "user", "text": "Hi"},
                {"role": "agent", "text": "Hello", "tool_calls": [{"tool": "lookup"}]},
            ],
        }
    )
    path = _write(tmp_path, "logs.jsonl", conversation.model_dump_json() + "\n\n")
    assert load_logs(path, LogFormat.JSONL) == [conversation]


def test_ws13_jsonl_names_the_bad_line(tmp_path: Path) -> None:
    path = _write(tmp_path, "logs.jsonl", '{"id": "c", "turns": []}\n')
    with pytest.raises(IngestError, match=r"logs\.jsonl:1"):
        load_logs(path, LogFormat.JSONL)


def test_ws13_duplicate_conversation_ids_are_rejected(tmp_path: Path) -> None:
    line = Conversation.model_validate(
        {"id": "c", "turns": [{"role": "user", "text": "x"}]}
    ).model_dump_json()
    path = _write(tmp_path, "logs.jsonl", f"{line}\n{line}\n")
    with pytest.raises(IngestError, match="appears twice"):
        load_logs(path, LogFormat.JSONL)


def test_ws13_missing_file_is_an_ingest_error(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="no such file"):
        load_logs(tmp_path / "nope.jsonl", LogFormat.JSONL)


# --- OpenTelemetry -------------------------------------------------------------------------


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"key": key, "value": {"stringValue": value}}
    return {"key": key, "value": {"intValue": str(value)}}


def _span(
    span_id: str,
    start_ns: int,
    *,
    parent: str | None = None,
    trace: str = "t1",
    status: dict[str, Any] | None = None,
    **attributes: Any,
) -> dict[str, Any]:
    span: dict[str, Any] = {
        "traceId": trace,
        "spanId": span_id,
        "name": "span",
        "startTimeUnixNano": str(start_ns),
        "attributes": [_attr(k.replace("__", "."), v) for k, v in attributes.items()],
    }
    if parent:
        span["parentSpanId"] = parent
    if status:
        span["status"] = status
    return span


def _messages(*messages: tuple[str, str]) -> str:
    return json.dumps(
        [{"role": role, "parts": [{"type": "text", "content": text}]} for role, text in messages]
    )


def _otlp(*spans: dict[str, Any]) -> str:
    return json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]})


SECOND = 1_000_000_000


def test_ws13_otel_genai_spans_rebuild_turns_and_tool_calls(tmp_path: Path) -> None:
    first_turn = _otlp(
        _span(
            "a1",
            100 * SECOND,
            gen_ai__operation__name="invoke_agent",
            gen_ai__conversation__id="conv-9",
            gen_ai__input__messages=_messages(("user", "Move my visit")),
            gen_ai__output__messages=_messages(("assistant", "Which day?")),
        ),
        # A nested model call must not produce a duplicate turn.
        _span(
            "c1",
            100 * SECOND + 5,
            parent="a1",
            gen_ai__operation__name="chat",
            gen_ai__input__messages=_messages(("user", "Move my visit")),
            gen_ai__output__messages=_messages(("assistant", "Which day?")),
        ),
        _span("h1", 99 * SECOND, gen_ai__operation__name="http"),  # ignored
    )
    second_turn = _otlp(
        _span(
            "a2",
            102 * SECOND,
            trace="t2",
            gen_ai__operation__name="invoke_agent",
            gen_ai__conversation__id="conv-9",
            gen_ai__input__messages=_messages(
                ("user", "Move my visit"), ("assistant", "Which day?"), ("user", "Friday")
            ),
            gen_ai__output__messages=_messages(("assistant", "Done, Friday 9am.")),
        ),
        _span(
            "x2",
            102 * SECOND + 10,
            trace="t2",
            parent="a2",
            gen_ai__operation__name="execute_tool",
            gen_ai__tool__name="reschedule",
            gen_ai__tool__call__arguments='{"day": "friday"}',
            gen_ai__tool__call__result='{"ok": true}',
        ),
    )
    path = _write(tmp_path, "spans.jsonl", f"{first_turn}\n{second_turn}\n")
    [conversation] = load_logs(path, LogFormat.OTEL)
    assert conversation.id == "conv-9"
    assert conversation.source == "otel"
    assert _dialogue(conversation) == [
        ("user", "Move my visit"),
        ("agent", "Which day?"),
        ("user", "Friday"),
        ("agent", "Done, Friday 9am."),
    ]
    call = conversation.turns[3].tool_calls[0]
    assert (call.tool, call.args, call.result) == ("reschedule", {"day": "friday"}, {"ok": True})
    assert [turn.t_ms for turn in conversation.turns] == [0, 0, 2000, 2000]


def test_ws13_otel_tools_outside_the_model_span_attach_to_the_next_reply(tmp_path: Path) -> None:
    document = _otlp(
        _span(
            "c1",
            1 * SECOND,
            gen_ai__operation__name="chat",
            gen_ai__input__messages=_messages(("user", "Refund please")),
            gen_ai__output__messages=json.dumps(
                [{"role": "assistant", "parts": [{"type": "tool_call", "name": "refund"}]}]
            ),
        ),
        _span(
            "x1",
            2 * SECOND,
            gen_ai__operation__name="execute_tool",
            gen_ai__tool__name="refund",
            gen_ai__tool__call__arguments="plain",
            status={"code": 2, "message": "timeout"},
        ),
        _span(
            "c2",
            3 * SECOND,
            gen_ai__operation__name="chat",
            gen_ai__input__messages=_messages(("user", "Refund please")),
            gen_ai__output__messages=_messages(("assistant", "Sorry, it failed.")),
        ),
        _span(
            "x9",
            4 * SECOND,
            gen_ai__operation__name="execute_tool",
            gen_ai__tool__name="log_case",
        ),
    )
    [conversation] = load_logs(_write(tmp_path, "spans.json", document), LogFormat.OTEL)
    assert conversation.id == "t1"
    assert _dialogue(conversation) == [
        ("user", "Refund please"),
        ("agent", "Sorry, it failed."),
        ("agent", ""),
    ]
    refund = conversation.turns[1].tool_calls[0]
    assert (refund.tool, refund.args, refund.error) == ("refund", {"input": "plain"}, "timeout")
    assert conversation.turns[2].tool_calls[0].tool == "log_case"


def test_ws13_otel_rejects_documents_without_spans(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="not OTLP JSON"):
        load_logs(_write(tmp_path, "x.json", '{"hello": 1}'), LogFormat.OTEL)


def test_ws13_otel_any_value_decodes_nested_values() -> None:
    value: JsonValue = {
        "kvlistValue": {
            "values": [
                {"key": "n", "value": {"intValue": "3"}},
                {"key": "xs", "value": {"arrayValue": {"values": [{"boolValue": True}]}}},
            ]
        }
    }
    assert any_value(value) == {"n": 3, "xs": [True]}
    assert any_value({"unknown": 1}) is None
    assert any_value("raw") == "raw"


# --- Langfuse ------------------------------------------------------------------------------


def test_ws13_langfuse_trace_export_groups_sessions(tmp_path: Path) -> None:
    rows = [
        {
            "id": "tr-1",
            "sessionId": "s-1",
            "timestamp": "2026-10-01T10:00:00Z",
            "input": {"messages": [{"role": "user", "content": "Where is my invoice?"}]},
            "output": {"role": "assistant", "content": "Let me check."},
            "metadata": {"csat": 2},
        },
        {
            "id": "tr-2",
            "sessionId": "s-1",
            "timestamp": "2026-10-01T10:00:05Z",
            "input": "It's INV-77",
            "output": "Found it: paid.",
        },
        {"id": "tr-3", "input": {"question": "Hours?"}, "output": {"answer": "9 to 5"}},
        {
            "id": "obs-1",
            "traceId": "tr-2",
            "type": "TOOL",
            "name": "find_invoice",
            "input": '{"number": "INV-77"}',
            "output": '{"status": "paid"}',
        },
        {
            "id": "obs-2",
            "traceId": "tr-2",
            "type": "GENERATION",
            "parentObservationId": "x",
            "input": "ignored nested generation",
        },
    ]
    path = _write(tmp_path, "langfuse.json", json.dumps({"data": rows}))
    session, single = load_logs(path, LogFormat.LANGFUSE)
    assert session.id == "s-1"
    assert session.metadata == {"csat": 2}
    assert _dialogue(session) == [
        ("user", "Where is my invoice?"),
        ("agent", "Let me check."),
        ("user", "It's INV-77"),
        ("agent", "Found it: paid."),
    ]
    assert session.turns[2].t_ms == 5000
    assert session.turns[3].tool_calls[0].args == {"number": "INV-77"}
    assert _dialogue(single) == [("user", "Hours?"), ("agent", "9 to 5")]


def test_ws13_langfuse_observation_rows_use_roots_and_tool_errors(tmp_path: Path) -> None:
    rows = [
        {
            "id": "o1",
            "traceId": "t",
            "type": "AGENT",
            "isRootObservation": True,
            "parentObservationId": "app",
            "startTime": "2026-10-01T10:00:00Z",
            "input": "Cancel order 5",
            "output": '{"content": "Cancelled."}',
        },
        {
            "id": "o2",
            "traceId": "t",
            "type": "TOOL",
            "name": "cancel",
            "level": "ERROR",
            "statusMessage": "boom",
        },
    ]
    lines = "\n".join(json.dumps(row) for row in rows)
    [conversation] = load_logs(_write(tmp_path, "obs.jsonl", lines), LogFormat.LANGFUSE)
    assert _dialogue(conversation) == [("user", "Cancel order 5"), ("agent", "Cancelled.")]
    assert conversation.turns[1].tool_calls[0].error == "boom"


# --- LangSmith -----------------------------------------------------------------------------


def _human(text: str) -> dict[str, Any]:
    return {"lc": 1, "type": "constructor", "id": ["langchain", "HumanMessage"],
            "kwargs": {"content": text, "type": "human"}}  # fmt: skip


def test_ws13_langsmith_runs_group_threads_and_attach_tools(tmp_path: Path) -> None:
    runs = [
        {
            "id": "r1",
            "trace_id": "r1",
            "run_type": "chain",
            "start_time": "2026-10-01T09:00:00",
            "inputs": {"messages": [[_human("Book a boiler check")]]},
            "outputs": {"messages": [{"type": "ai", "content": "Which postcode?"}]},
            "extra": {"metadata": {"thread_id": "th-1", "plan": "gold"}},
        },
        {
            "id": "r2",
            "trace_id": "r2",
            "run_type": "chain",
            "start_time": "2026-10-01T09:00:03",
            "inputs": {"input": "92000"},
            "outputs": {"output": "Booked for Monday."},
            "extra": {"metadata": {"thread_id": "th-1"}},
        },
        {
            "id": "r3",
            "trace_id": "r2",
            "parent_run_id": "r2",
            "run_type": "tool",
            "name": "book",
            "inputs": {"input": '{"postcode": "92000"}'},
            "outputs": {"output": "ok"},
        },
        {
            "id": "r4",
            "trace_id": "r2",
            "parent_run_id": "r2",
            "run_type": "llm",
            "inputs": {"messages": []},
        },
    ]
    [conversation] = load_logs(_write(tmp_path, "runs.json", json.dumps(runs)), LogFormat.LANGSMITH)
    assert conversation.id == "th-1"
    assert conversation.metadata == {"plan": "gold"}
    assert _dialogue(conversation) == [
        ("user", "Book a boiler check"),
        ("agent", "Which postcode?"),
        ("user", "92000"),
        ("agent", "Booked for Monday."),
    ]
    call = conversation.turns[3].tool_calls[0]
    assert (call.tool, call.args, call.result) == ("book", {"postcode": "92000"}, "ok")
    assert conversation.started_at is not None
    assert conversation.started_at.tzinfo is not None


def test_ws13_langsmith_run_without_messages_is_an_error(tmp_path: Path) -> None:
    runs = [{"id": "r1", "run_type": "chain", "inputs": {}, "outputs": {}}]
    with pytest.raises(IngestError, match="no messages"):
        load_logs(_write(tmp_path, "runs.json", json.dumps(runs)), LogFormat.LANGSMITH)


# --- CSV -----------------------------------------------------------------------------------


def test_ws13_csv_mapper_reads_custom_columns(tmp_path: Path) -> None:
    csv_text = (
        "chat;who;body;at;plan\n"
        "c1;Customer;Hello;2026-10-01T10:00:02;gold\n"
        "c1;Bot;Hi there;2026-10-01T10:00:03;gold\n"
        "c1;Customer;First;2026-10-01T10:00:00;gold\n"
        "c2;customer;Only me;2026-10-01T11:00:00;free\n"
    )
    mapping_path = _write(
        tmp_path,
        "mapping.yaml",
        "conversation_id: chat\nrole: who\ntext: body\ntimestamp: at\n"
        "metadata: [plan]\ndelimiter: ';'\nrole_values: {customer: user, bot: agent}\n",
    )
    mapping = CsvMapping.from_yaml(mapping_path)
    logs = _write(tmp_path, "logs.csv", csv_text)
    first, second = load_logs(logs, LogFormat.CSV, mapping=mapping)
    assert _dialogue(first) == [("user", "First"), ("user", "Hello"), ("agent", "Hi there")]
    assert [turn.t_ms for turn in first.turns] == [0, 2000, 3000]
    assert first.metadata == {"plan": "gold"}
    assert second.source == "csv"
    assert second.channel is Channel.CHAT


def test_ws13_csv_defaults_and_channel_column(tmp_path: Path) -> None:
    csv_text = "conversation_id,role,text,channel\nc1,user,hi,email\nc1,assistant,hello,email\n"
    [conversation] = load_logs(
        _write(tmp_path, "logs.csv", csv_text),
        LogFormat.CSV,
        mapping=CsvMapping(channel="channel"),
    )
    assert conversation.channel is Channel.EMAIL
    assert all(turn.t_ms is None for turn in conversation.turns)


@pytest.mark.parametrize(
    ("csv_text", "mapping", "message"),
    [
        ("id,role,text\n", CsvMapping(), "missing CSV column"),
        ("conversation_id,role,text\nc,robot,x\n", CsvMapping(), "unknown role 'robot'"),
        (
            "conversation_id,role,text,at\nc,user,x,yesterday\n",
            CsvMapping(timestamp="at"),
            "unreadable timestamp",
        ),
        (
            "conversation_id,role,text,ch\nc,user,x,fax\n",
            CsvMapping(channel="ch"),
            "conversation 'c'",
        ),
    ],
)
def test_ws13_csv_errors_name_the_problem(
    tmp_path: Path, csv_text: str, mapping: CsvMapping, message: str
) -> None:
    with pytest.raises(IngestError, match=message):
        load_logs(_write(tmp_path, "logs.csv", csv_text), LogFormat.CSV, mapping=mapping)


def test_ws13_invalid_mapping_file_is_an_ingest_error(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="invalid CSV mapping"):
        CsvMapping.from_yaml(_write(tmp_path, "m.yaml", "colour: blue\n"))


# --- shared helpers ------------------------------------------------------------------------


def test_ws13_read_records_accepts_arrays_objects_and_lines(tmp_path: Path) -> None:
    assert read_records(_write(tmp_path, "a.json", '[{"a": 1}]')) == [{"a": 1}]
    assert read_records(_write(tmp_path, "b.json", '{"a": 1}')) == [{"a": 1}]
    assert read_records(_write(tmp_path, "c.json", "  ")) == []
    with pytest.raises(IngestError, match="expected JSON objects"):
        read_records(_write(tmp_path, "d.json", "3"))
    with pytest.raises(IngestError, match=r"e\.jsonl:2"):
        read_records(_write(tmp_path, "e.jsonl", "{}\n{oops\n"))
    with pytest.raises(IngestError, match="record 1 is not a JSON object"):
        read_records(_write(tmp_path, "f.json", "[1]"))


def test_ws13_message_text_handles_common_shapes() -> None:
    assert text_of([{"type": "text", "text": "a"}, {"type": "image"}, "b"]) == "ab"
    assert text_of('[{"type": "text", "content": "c"}]') == "c"
    assert text_of({"message": "d"}) == "d"
    assert text_of(3) == ""
    found = messages_in({"generations": [[{"message": {"role": "system", "content": "x"}}]]})
    assert [m.role for m in found] == ["other"]


def test_ws13_parse_time_accepts_iso_and_epoch_nanoseconds() -> None:
    assert parse_time("2026-10-01T10:00:00Z") == parse_time(1_790_848_800 * SECOND)
    assert parse_time(str(1_790_848_800 * SECOND)) == parse_time("2026-10-01T10:00:00+00:00")
    assert parse_time("not a time") is None
    assert parse_time(None) is None
