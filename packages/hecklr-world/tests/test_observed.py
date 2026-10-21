"""Observed mode: tool calls read from OTLP/HTTP JSON GenAI spans."""

import base64
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx2
import pytest

from hecklr_schemas import TRIAL_ID_HEADER, FakeClock, TrialBrief, World
from hecklr_world import Sandbox
from hecklr_world.otlp import ExportTraceRequest, any_value, tool_spans

pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

BriefFactory = Callable[..., TrialBrief]
SinkFactory = Callable[..., Any]


def _attr(key: str, value: Any) -> dict[str, Any]:
    if isinstance(value, bool):
        return {"key": key, "value": {"boolValue": value}}
    if isinstance(value, int):
        return {"key": key, "value": {"intValue": str(value)}}
    return {"key": key, "value": {"stringValue": value}}


def _span(
    tool: str,
    *,
    start_ns: int,
    duration_ms: int,
    attributes: list[dict[str, Any]] | None = None,
    status: dict[str, Any] | None = None,
    span_id: str = "a1b2c3d4e5f60718",
) -> dict[str, Any]:
    return {
        "traceId": "0af7651916cd43dd8448eb211c80319c",
        "spanId": span_id,
        "name": f"execute_tool {tool}",
        "kind": 1,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(start_ns + duration_ms * 1_000_000),
        "attributes": [
            _attr("gen_ai.operation.name", "execute_tool"),
            _attr("gen_ai.tool.name", tool),
            *(attributes or []),
        ],
        "status": status or {},
    }


def _export(
    spans: list[dict[str, Any]], resource: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": resource or [_attr("service.name", "acme-agent")]},
                "scopeSpans": [{"scope": {"name": "acme.tools"}, "spans": spans}],
            }
        ]
    }


@pytest.fixture
async def sandbox() -> AsyncIterator[Sandbox]:
    async with Sandbox(tool_mode="observed") as running:
        yield running


@pytest.mark.anyio
async def test_observed_sessions_have_no_tool_urls(
    sandbox: Sandbox, example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief()
    world = await sandbox.open(brief, example_world, make_sink(brief.trial_id))
    assert (world.mcp_url, world.http_url) == (None, None)
    assert sandbox.tool_mode == "observed"
    assert sandbox.otlp_endpoint == sandbox.base_url
    async with httpx2.AsyncClient() as http:
        tools = await http.post(
            f"{sandbox.base_url}/t/{brief.trial_id}/tools/get_customer", json={"customer_id": "C"}
        )
    assert tools.status_code == 404


@pytest.mark.anyio
async def test_genai_tool_spans_become_observed_tool_events(
    sandbox: Sandbox, example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief()
    clock = FakeClock(start_ms=10_000)
    sink = make_sink(brief.trial_id, clock)
    world = await sandbox.open(brief, example_world, sink)
    now_ns = time.time_ns()
    clock.advance(60_000)  # the export arrives after the calls happened
    spans = [
        _span(
            "get_customer",
            start_ns=now_ns + 2_000_000_000,
            duration_ms=35,
            attributes=[
                _attr("hecklr.trial_id", brief.trial_id),
                _attr("gen_ai.tool.call.id", "call_1"),
                _attr("gen_ai.tool.call.arguments", json.dumps({"customer_id": "C-1042"})),
                _attr("gen_ai.tool.call.result", json.dumps({"name": "Camille"})),
            ],
        ),
        _span(
            "issue_refund",
            start_ns=now_ns + 3_000_000_000,
            duration_ms=120,
            attributes=[
                _attr("hecklr.trial_id", brief.trial_id),
                _attr("error.type", "PaymentDeclined"),
            ],
            status={"code": 2, "message": "card declined"},
        ),
        {
            "name": "chat gpt-5",
            "startTimeUnixNano": str(now_ns),
            "endTimeUnixNano": str(now_ns),
            "attributes": [_attr("gen_ai.operation.name", "chat")],
        },
    ]
    async with httpx2.AsyncClient() as http:
        response = await http.post(f"{sandbox.otlp_endpoint}/v1/traces", json=_export(spans))
    assert response.status_code == 200
    assert response.json() == {}

    call, result, failed_call, failed = sink.events
    assert call.payload.kind == "tool_call"
    assert (call.payload.source, call.payload.call_id) == ("observed", "call_1")
    assert call.payload.args == {"customer_id": "C-1042"}
    # Span times map onto the trial clock through the wall time at open (a few ms of slack).
    assert 12_000 <= call.t_ms <= 12_010
    assert result.payload.result == {"name": "Camille"}
    assert (result.t_ms - call.t_ms, result.payload.latency_ms, result.payload.error) == (
        35,
        35,
        None,
    )
    assert failed_call.payload.tool == "issue_refund"
    assert failed_call.payload.call_id == "a1b2c3d4e5f60718"
    assert failed_call.payload.args == {}
    assert failed.payload.error == "card declined"
    assert failed.payload.latency_ms == 120
    assert world.snapshot().collections["refunds"] == {}


@pytest.mark.anyio
async def test_spans_join_trials_by_header_resource_or_captured_header(
    sandbox: Sandbox, example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    briefs = [make_brief() for _ in range(3)]
    sinks = [make_sink(b.trial_id) for b in briefs]
    for brief, sink in zip(briefs, sinks, strict=True):
        await sandbox.open(brief, example_world, sink)
    now = time.time_ns()
    by_captured_header = _span(
        "get_policy",
        start_ns=now,
        duration_ms=1,
        attributes=[
            {
                "key": "http.request.header.x-hecklr-trial-id",
                "value": {"arrayValue": {"values": [{"stringValue": briefs[2].trial_id}]}},
            }
        ],
    )
    async with httpx2.AsyncClient() as http:
        await http.post(
            f"{sandbox.base_url}/v1/traces",
            json=_export([_span("get_customer", start_ns=now, duration_ms=1)]),
            headers={TRIAL_ID_HEADER: briefs[0].trial_id},
        )
        await http.post(
            f"{sandbox.base_url}/v1/traces",
            json=_export(
                [_span("list_appointments", start_ns=now, duration_ms=1)],
                resource=[_attr("hecklr.trial_id", briefs[1].trial_id)],
            ),
        )
        await http.post(f"{sandbox.base_url}/v1/traces", json=_export([by_captured_header]))
    assert [[c.tool for c in sink.calls()] for sink in sinks] == [
        ["get_customer"],
        ["list_appointments"],
        ["get_policy"],
    ]


@pytest.mark.anyio
async def test_unattributed_or_unknown_spans_are_reported_as_rejected(
    sandbox: Sandbox, example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief()
    sink = make_sink(brief.trial_id)
    world = await sandbox.open(brief, example_world, sink)
    await world.close()
    now = time.time_ns()
    spans = [
        _span("get_customer", start_ns=now, duration_ms=1),
        _span(
            "get_customer",
            start_ns=now,
            duration_ms=1,
            attributes=[_attr("hecklr.trial_id", brief.trial_id)],
        ),
    ]
    async with httpx2.AsyncClient() as http:
        response = await http.post(f"{sandbox.base_url}/v1/traces", json=_export(spans))
    assert response.json() == {
        "partialSuccess": {
            "rejectedSpans": "2",
            "errorMessage": "2 tool span(s) matched no open observed-mode trial",
        }
    }
    assert sink.events == []


@pytest.mark.anyio
async def test_traces_must_be_otlp_json(sandbox: Sandbox) -> None:
    async with httpx2.AsyncClient() as http:
        protobuf = await http.post(
            f"{sandbox.base_url}/v1/traces",
            content=b"\x0a\x00",
            headers={"content-type": "application/x-protobuf"},
        )
        malformed = await http.post(
            f"{sandbox.base_url}/v1/traces", json={"resourceSpans": [{"scopeSpans": 3}]}
        )
    assert protobuf.status_code == 415
    assert malformed.status_code == 400


@pytest.mark.anyio
async def test_sandbox_mode_rejects_observed_spans(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief()
    sink = make_sink(brief.trial_id)
    async with Sandbox() as sandbox, httpx2.AsyncClient() as http:
        await sandbox.open(brief, example_world, sink)
        span = _span(
            "get_customer",
            start_ns=time.time_ns(),
            duration_ms=1,
            attributes=[_attr("hecklr.trial_id", brief.trial_id)],
        )
        response = await http.post(f"{sandbox.base_url}/v1/traces", json=_export([span]))
    assert response.json()["partialSuccess"]["rejectedSpans"] == "1"
    assert sink.events == []


def test_any_value_decodes_every_otlp_json_value_kind() -> None:
    assert any_value({"stringValue": "a"}) == "a"
    assert any_value({"boolValue": True}) is True
    assert any_value({"intValue": "9007199254740993"}) == 9007199254740993
    assert any_value({"doubleValue": 1.5}) == 1.5
    assert any_value({"bytesValue": base64.b64encode(b"hi").decode()}) == "hi"
    assert any_value({"arrayValue": {"values": [{"intValue": "1"}, {"stringValue": "x"}]}}) == [
        1,
        "x",
    ]
    assert any_value({"arrayValue": {}}) == []
    assert any_value(
        {"kvlistValue": {"values": [{"key": "customer_id", "value": {"stringValue": "C-1"}}]}}
    ) == {"customer_id": "C-1"}
    assert any_value({}) is None


def test_structured_or_plain_arguments_and_results_are_accepted() -> None:
    span = _span(
        "lookup",
        start_ns=0,
        duration_ms=1,
        attributes=[
            {
                "key": "gen_ai.tool.call.arguments",
                "value": {"kvlistValue": {"values": [{"key": "q", "value": {"stringValue": "x"}}]}},
            },
            _attr("gen_ai.tool.call.result", "plain text, not JSON"),
        ],
    )
    unnamed = _span(
        "ignored",
        start_ns=0,
        duration_ms=1,
        attributes=[_attr("gen_ai.tool.call.arguments", "[1, 2]")],
    )
    unnamed["attributes"] = [a for a in unnamed["attributes"] if a["key"] != "gen_ai.tool.name"]
    unnamed["status"] = {"code": "STATUS_CODE_ERROR"}
    request = ExportTraceRequest.model_validate(_export([span, unnamed]))
    first, second = tool_spans(request, header_trial_id="T")
    assert first.trial_id == "T"
    assert (first.call.args, first.call.result) == ({"q": "x"}, "plain text, not JSON")
    assert (second.call.tool, second.call.args, second.call.error) == (
        "ignored",
        {"value": [1, 2]},
        "error",
    )
