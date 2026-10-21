"""The shared server's OpenAPI HTTP surface, driven by a real HTTP client on localhost."""

from collections.abc import AsyncIterator, Callable
from typing import Any

import anyio
import httpx2
import pytest

from hecklr_schemas import SessionContext, TrialBrief, World, WorldSandbox
from hecklr_world import Sandbox

pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

BriefFactory = Callable[..., TrialBrief]
SinkFactory = Callable[..., Any]


@pytest.fixture
async def sandbox() -> AsyncIterator[Sandbox]:
    async with Sandbox() as running:
        yield running


@pytest.fixture
async def client() -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(timeout=5.0) as http:
        yield http


def test_sandbox_implements_the_world_sandbox_protocol() -> None:
    assert isinstance(Sandbox(), WorldSandbox)


@pytest.mark.anyio
async def test_each_trial_gets_urls_on_one_shared_port(
    sandbox: Sandbox, example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    briefs = [make_brief(), make_brief()]
    worlds = [await sandbox.open(b, example_world, make_sink(b.trial_id)) for b in briefs]
    for brief, world in zip(briefs, worlds, strict=True):
        assert world.http_url == f"{sandbox.base_url}/t/{brief.trial_id}"
        assert world.mcp_url == f"{sandbox.base_url}/t/{brief.trial_id}/mcp"
        ctx = SessionContext(
            trial_id=brief.trial_id,
            seed=brief.seed,
            world_mcp_url=world.mcp_url,
            world_http_url=world.http_url,
        )
        assert ctx.world_http_url == world.http_url
    assert sorted(sandbox.open_trials) == sorted(b.trial_id for b in briefs)
    await worlds[0].close()
    assert sandbox.open_trials == [briefs[1].trial_id]


@pytest.mark.anyio
async def test_openapi_document_lists_every_tool_with_its_schema(
    sandbox: Sandbox,
    client: httpx2.AsyncClient,
    example_world: World,
    make_brief: BriefFactory,
    make_sink: SinkFactory,
) -> None:
    brief = make_brief()
    world = await sandbox.open(brief, example_world, make_sink(brief.trial_id))
    response = await client.get(f"{world.http_url}/openapi.json")
    assert response.status_code == 200
    doc = response.json()
    assert doc["openapi"] == "3.1.0"
    assert doc["servers"] == [{"url": world.http_url}]
    assert sorted(doc["paths"]) == sorted(f"/tools/{t.name}" for t in example_world.tools)
    reschedule = doc["paths"]["/tools/reschedule_appointment"]["post"]
    assert reschedule["operationId"] == "reschedule_appointment"
    assert reschedule["description"] == (
        "Move an appointment to a new slot (ISO 8601, YYYY-MM-DDTHH:MM)."
    )
    schema = reschedule["requestBody"]["content"]["application/json"]["schema"]
    assert schema["required"] == ["appointment_id", "slot"]


@pytest.mark.anyio
async def test_tool_endpoints_change_state_and_return_structured_errors(
    sandbox: Sandbox,
    client: httpx2.AsyncClient,
    example_world: World,
    make_brief: BriefFactory,
    make_sink: SinkFactory,
) -> None:
    brief = make_brief()
    sink = make_sink(brief.trial_id)
    world = await sandbox.open(brief, example_world, sink)
    ok = await client.post(
        f"{world.http_url}/tools/reschedule_appointment",
        json={"appointment_id": "A-1", "slot": "2026-10-28T09:00"},
    )
    missing = await client.post(f"{world.http_url}/tools/get_customer", json={"customer_id": "X"})
    invalid = await client.post(f"{world.http_url}/tools/get_customer", json={})
    not_json = await client.post(f"{world.http_url}/tools/get_customer", content=b"{nope")
    not_object = await client.post(f"{world.http_url}/tools/get_customer", json=[1])
    unknown = await client.post(f"{world.http_url}/tools/teleport", content=b"")

    assert (ok.status_code, ok.json()["slot"]) == (200, "2026-10-28T09:00")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"
    assert (invalid.status_code, invalid.json()["error"]["code"]) == (400, "invalid_arguments")
    assert not_json.status_code == 400
    assert not_json.json()["error"]["message"].startswith("request body is not JSON")
    assert not_object.json()["error"]["message"] == (
        "the request body must be a JSON object of tool arguments"
    )
    assert (unknown.status_code, unknown.json()["error"]["code"]) == (404, "unknown_tool")
    assert world.snapshot().collections["appointments"]["A-1"]["slot"] == "2026-10-28T09:00"
    assert [c.tool for c in sink.calls()] == [
        "reschedule_appointment",
        "get_customer",
        "get_customer",
        "teleport",
    ]


@pytest.mark.anyio
async def test_unknown_or_closed_trials_are_404(
    sandbox: Sandbox,
    client: httpx2.AsyncClient,
    example_world: World,
    make_brief: BriefFactory,
    make_sink: SinkFactory,
) -> None:
    brief = make_brief()
    world = await sandbox.open(brief, example_world, make_sink(brief.trial_id))
    await world.close()
    for url in (f"{world.http_url}/tools/get_customer", f"{sandbox.base_url}/t/NOPE/tools/x"):
        response = await client.post(url, json={"customer_id": "C-1042"})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    assert (await client.get(f"{world.http_url}/openapi.json")).status_code == 404


@pytest.mark.anyio
async def test_http_500_fault_answers_with_status_500(
    sandbox: Sandbox,
    client: httpx2.AsyncClient,
    example_world: World,
    make_brief: BriefFactory,
    make_sink: SinkFactory,
) -> None:
    brief = make_brief(faults=[{"tool": "get_customer", "kind": "http_500"}])
    world = await sandbox.open(brief, example_world, make_sink(brief.trial_id))
    response = await client.post(f"{world.http_url}/tools/get_customer", json={"customer_id": "C"})
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Internal Server Error"}
    }


@pytest.mark.anyio
async def test_timeout_fault_makes_the_agents_own_timeout_fire(
    sandbox: Sandbox,
    client: httpx2.AsyncClient,
    example_world: World,
    make_brief: BriefFactory,
    make_sink: SinkFactory,
) -> None:
    brief = make_brief(faults=[{"tool": "get_customer", "kind": "timeout"}])
    sink = make_sink(brief.trial_id)
    world = await sandbox.open(brief, example_world, sink)
    with pytest.raises(httpx2.ReadTimeout):
        await client.post(
            f"{world.http_url}/tools/get_customer", json={"customer_id": "C-1042"}, timeout=0.3
        )
    await world.close()
    [result] = sink.results()
    assert result.fault == "timeout"


@pytest.mark.anyio
async def test_thirty_two_concurrent_trials_are_isolated_over_http(
    sandbox: Sandbox,
    client: httpx2.AsyncClient,
    example_world: World,
    make_brief: BriefFactory,
    make_sink: SinkFactory,
) -> None:
    briefs = [make_brief(seed=i) for i in range(32)]
    worlds = [await sandbox.open(b, example_world, make_sink(b.trial_id)) for b in briefs]

    async def book(index: int) -> None:
        url = worlds[index].http_url
        slot = f"2026-11-{index + 1:02d}T09:00"
        for _ in range(3):
            response = await client.post(
                f"{url}/tools/reschedule_appointment", json={"appointment_id": "A-1", "slot": slot}
            )
            assert response.status_code == 200
        await client.post(
            f"{url}/tools/issue_refund", json={"invoice_id": "INV-501", "amount_eur": index + 1}
        )

    async with anyio.create_task_group() as tg:
        for index in range(32):
            tg.start_soon(book, index)

    for index, world in enumerate(worlds):
        state = world.snapshot().collections
        assert state["appointments"]["A-1"]["slot"] == f"2026-11-{index + 1:02d}T09:00"
        assert state["refunds"] == {
            "refunds-1": {
                "invoice_id": "INV-501",
                "amount_eur": index + 1,
                "refund_id": "refunds-1",
            }
        }


@pytest.mark.anyio
async def test_leaving_the_sandbox_closes_open_trials_and_stops_the_server(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief()
    async with Sandbox() as sandbox:
        world = await sandbox.open(brief, example_world, make_sink(brief.trial_id))
        base_url = sandbox.base_url
    assert world.closed
    with pytest.raises(RuntimeError, match="not running"):
        _ = sandbox.base_url
    async with httpx2.AsyncClient() as http:
        with pytest.raises(httpx2.ConnectError):
            await http.get(f"{base_url}/t/x/openapi.json")


@pytest.mark.anyio
async def test_a_sandbox_cannot_open_the_same_trial_twice_or_run_twice(
    sandbox: Sandbox, example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief()
    await sandbox.open(brief, example_world, make_sink(brief.trial_id))
    with pytest.raises(ValueError, match="already has an open world"):
        await sandbox.open(brief, example_world, make_sink(brief.trial_id))
    with pytest.raises(RuntimeError, match="already running"):
        await sandbox.__aenter__()


@pytest.mark.anyio
async def test_opening_a_world_needs_a_running_sandbox(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief()
    with pytest.raises(RuntimeError, match="not running"):
        await Sandbox().open(brief, example_world, make_sink(brief.trial_id))
