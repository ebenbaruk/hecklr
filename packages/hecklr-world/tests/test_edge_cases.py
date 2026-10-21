"""Races and failure paths that ordinary traffic rarely reaches."""

from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from mcp import Client, MCPError
from mcp.client.streamable_http import streamable_http_client

from hecklr_schemas import TrialBrief, World
from hecklr_world import ObservedToolCall, Sandbox, SessionClosedError, TrialWorld
from hecklr_world.app import create_app

BriefFactory = Callable[..., TrialBrief]
SinkFactory = Callable[..., Any]


@pytest.mark.anyio
async def test_calls_racing_a_close_get_a_clean_not_found(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    # A session closed between routing and the call: the registry still lists it.
    brief = make_brief()
    trial = TrialWorld(brief, example_world, make_sink(brief.trial_id))
    await trial.close()
    app = create_app({brief.trial_id: trial}, allowed_hosts=["testserver"])
    transport = httpx2.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(transport=transport, base_url="http://testserver") as http,
    ):
        response = await http.post(f"/t/{brief.trial_id}/tools/get_customer", json={})
        assert response.status_code == 404
        assert "is closed" in response.json()["error"]["message"]
        mcp_url = f"http://testserver/t/{brief.trial_id}/mcp"
        async with Client(streamable_http_client(mcp_url, http_client=http)) as client:
            with pytest.raises(MCPError, match="is closed"):
                await client.call_tool("get_customer", {"customer_id": "C-1042"})


@pytest.mark.anyio
async def test_observed_calls_after_close_are_refused(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief()
    trial = TrialWorld(brief, example_world, make_sink(brief.trial_id), observed=True)
    await trial.close()
    call = ObservedToolCall("t", "c", {}, None, None, 0, 0)
    with pytest.raises(SessionClosedError):
        trial.record_observed(call)


@pytest.mark.anyio
async def test_a_key_param_missing_from_the_arguments_is_an_argument_error(
    make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    world = World.model_validate(
        {
            "id": "w",
            "collections": {"items": {"records": [{"id": "a"}]}},
            "tools": [
                {
                    "name": "get_item",
                    "description": "d",
                    "op": {"kind": "get", "collection": "items"},
                }
            ],
        }
    )
    brief = make_brief()
    trial = TrialWorld(brief, world, make_sink(brief.trial_id))
    outcome = await trial.call_tool("get_item", {})
    assert outcome.body() == {
        "error": {"code": "invalid_arguments", "message": "missing required argument 'id'"}
    }


@pytest.mark.anyio
@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_a_server_that_does_not_start_in_time_is_torn_down() -> None:
    sandbox = Sandbox(startup_timeout_s=0)
    with pytest.raises(TimeoutError):
        await sandbox.__aenter__()
    with pytest.raises(RuntimeError, match="not running"):
        _ = sandbox.base_url
