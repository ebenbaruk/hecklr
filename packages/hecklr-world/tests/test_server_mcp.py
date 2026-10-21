"""The MCP endpoint, driven by the official MCP SDK client over streamable HTTP on localhost."""

import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx2
import pytest
from mcp import Client, MCPError
from mcp.types import TextContent

from hecklr_schemas import TrialBrief, World
from hecklr_world import Sandbox, TrialWorld

pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

BriefFactory = Callable[..., TrialBrief]
SinkFactory = Callable[..., Any]


@pytest.fixture
async def sandbox() -> AsyncIterator[Sandbox]:
    async with Sandbox() as running:
        yield running


def _text(result: Any) -> Any:
    [block] = result.content
    assert isinstance(block, TextContent)
    return json.loads(block.text)


def _url(world: TrialWorld) -> str:
    assert world.mcp_url is not None
    return world.mcp_url


@pytest.mark.anyio
async def test_mcp_lists_the_worlds_tools_with_their_schemas(
    sandbox: Sandbox, example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief()
    world = await sandbox.open(brief, example_world, make_sink(brief.trial_id))
    async with Client(_url(world)) as client:
        listed = await client.list_tools()
    tools = {tool.name: tool for tool in listed.tools}
    assert list(tools) == [tool.name for tool in example_world.tools]
    assert tools["get_policy"].description == "Read a company policy such as cancellation fees."
    assert tools["get_customer"].input_schema["required"] == ["customer_id"]


@pytest.mark.anyio
async def test_mcp_tool_calls_change_this_trials_state_and_are_recorded(
    sandbox: Sandbox, example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief()
    sink = make_sink(brief.trial_id)
    world = await sandbox.open(brief, example_world, sink)
    async with Client(_url(world)) as client:
        moved = await client.call_tool(
            "reschedule_appointment", {"appointment_id": "A-1", "slot": "2026-10-28T09:00"}
        )
        listed = await client.call_tool("list_appointments", {"customer_id": "C-2077"})
    assert not moved.is_error
    assert _text(moved)["slot"] == "2026-10-28T09:00"
    assert moved.structured_content == {"result": _text(moved)}
    assert [r["appointment_id"] for r in _text(listed)] == ["A-3"]
    assert world.snapshot().collections["appointments"]["A-1"]["slot"] == "2026-10-28T09:00"
    assert [c.tool for c in sink.calls()] == ["reschedule_appointment", "list_appointments"]
    assert all(r.ok for r in sink.results())


@pytest.mark.anyio
async def test_mcp_tool_errors_and_faults_are_tool_errors_the_model_can_read(
    sandbox: Sandbox, example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief(faults=[{"tool": "get_policy", "kind": "http_500"}])
    world = await sandbox.open(brief, example_world, make_sink(brief.trial_id))
    async with Client(_url(world)) as client:
        invalid = await client.call_tool("get_customer", {"customer_id": 7})
        faulted = await client.call_tool("get_policy", {"policy_id": "cancellation"})
    assert invalid.is_error
    assert _text(invalid)["error"]["code"] == "invalid_arguments"
    assert invalid.structured_content is None
    assert faulted.is_error
    assert _text(faulted) == {
        "error": {"code": "internal_error", "message": "Internal Server Error"}
    }


@pytest.mark.anyio
async def test_mcp_trials_are_isolated_from_each_other(
    sandbox: Sandbox, example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    first, second = make_brief(), make_brief()
    world_a = await sandbox.open(first, example_world, make_sink(first.trial_id))
    world_b = await sandbox.open(second, example_world, make_sink(second.trial_id))
    async with Client(_url(world_a)) as client:
        await client.call_tool("issue_refund", {"invoice_id": "INV-501", "amount_eur": 40})
    assert list(world_a.snapshot().collections["refunds"]) == ["refunds-1"]
    assert world_b.snapshot().collections["refunds"] == {}


@pytest.mark.anyio
async def test_mcp_requests_for_unknown_trials_are_protocol_errors(sandbox: Sandbox) -> None:
    async with Client(f"{sandbox.base_url}/t/NOPE/mcp") as client:
        with pytest.raises(MCPError, match="no open trial world 'NOPE'"):
            await client.list_tools()
        with pytest.raises(MCPError, match="no open trial world 'NOPE'"):
            await client.call_tool("get_customer", {"customer_id": "C-1042"})


@pytest.mark.anyio
async def test_the_mcp_url_answers_without_a_redirect(
    sandbox: Sandbox, example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    # Clients that do not follow a 307 on POST must work against mcp_url exactly as given.
    brief = make_brief()
    world = await sandbox.open(brief, example_world, make_sink(brief.trial_id))
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_customer", "arguments": {"customer_id": "C-1042"}},
    }
    headers = {
        "accept": "application/json, text/event-stream",
        "mcp-protocol-version": "2025-06-18",
    }
    async with httpx2.AsyncClient(follow_redirects=False) as http:
        response = await http.post(_url(world), json=request, headers=headers)
    assert response.status_code == 200
    result = response.json()["result"]
    assert json.loads(result["content"][0]["text"])["name"] == "Camille Durand"
