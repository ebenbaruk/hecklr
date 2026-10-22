"""The agent as an HTTP service, driven by Hecklr's own ``http`` adapter (JSON and SSE).

Servers run on 127.0.0.1 ephemeral ports: these tests get localhost sockets only.
"""

import contextlib
import socket
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager

import anyio
import httpx2
import pytest
import uvicorn
from acme_agent import (
    AcmeAgent,
    AgentSettings,
    InProcessAgent,
    create_app,
    load_agent_files,
)
from acme_agent.server import sse_frame
from acme_testing import ListSink
from fastapi import FastAPI

from hecklr_adapters import WORLD_URL_HEADER, AgentSession, HttpChatTarget
from hecklr_schemas import (
    TRIAL_ID_HEADER,
    AgentChunk,
    Channel,
    ChatSession,
    HttpAdapterConfig,
    Persona,
    Scenario,
    SessionContext,
    ToolCallPayload,
    TrialBrief,
    TrialId,
    World,
    new_id,
)
from hecklr_world import Sandbox

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.enable_socket,
    pytest.mark.allow_hosts(["127.0.0.1"]),
]


class _QuietServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to pytest."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


@asynccontextmanager
async def serving(app: FastAPI) -> AsyncIterator[str]:
    """Serve ``app`` on an ephemeral localhost port for the duration of the block."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = _QuietServer(uvicorn.Config(app, log_config=None, access_log=False))
    async with anyio.create_task_group() as group:
        group.start_soon(server.serve, [sock])
        with anyio.fail_after(10):
            while not server.started:  # noqa: ASYNC110 -- uvicorn exposes readiness only as a flag
                await anyio.sleep(0.01)
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.should_exit = True
    sock.close()


def rules_app(
    world: World,
    world_base_url: str | None = None,
    world_transport: httpx2.AsyncBaseTransport | None = None,
) -> FastAPI:
    settings = AgentSettings(brain="rules", world_base_url=world_base_url)
    agent = AcmeAgent(load_agent_files(), settings)
    return create_app(agent, world, world_transport=world_transport)


def refusing_transport(seen: list[httpx2.Request]) -> httpx2.MockTransport:
    """A world sandbox nobody can connect to, without a real socket: this machine's loopback is
    shared with parallel test workers (and endpoint-security filtering), which made a real
    refused port an unreliable way to fail."""

    def refuse(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        raise httpx2.ConnectError("connection refused", request=request)

    return httpx2.MockTransport(refuse)


async def turn(session: ChatSession, text: str) -> list[AgentChunk]:
    return [chunk async for chunk in session.send(text)]


def http_config(url: str, **options: str) -> HttpAdapterConfig:
    return HttpAdapterConfig.model_validate({"type": "http", "url": url, **options})


def context() -> SessionContext:
    return SessionContext(trial_id=TrialId(new_id()), seed=7)


async def test_sse_stream_carries_text_tools_handoff_and_end(world: World) -> None:
    async with serving(rules_app(world)) as url:
        target = HttpChatTarget(http_config(f"{url}/chat/stream", stream="sse"))
        session = await target.open(context())
        chunks = await turn(session, "I can smell gas in the hallway!")
        await session.close()
    text = "".join(chunk.text for chunk in chunks)
    assert text.startswith("This is an emergency. Please leave the property now")
    assert len([chunk for chunk in chunks if chunk.text]) > 10  # streamed word by word
    [call] = [chunk.tool_call for chunk in chunks if chunk.tool_call is not None]
    assert call.tool == "create_emergency_ticket"
    [result] = [chunk.tool_result for chunk in chunks if chunk.tool_result is not None]
    assert result.call_id == call.call_id
    assert result.error is None
    [handoff] = [chunk.handoff for chunk in chunks if chunk.handoff is not None]
    assert handoff.target == "on-call engineer"
    assert any(chunk.ended for chunk in chunks)
    assert chunks[-1].final


async def test_json_endpoint_keeps_the_conversation(world: World) -> None:
    async with serving(rules_app(world)) as url:
        config = http_config(f"{url}/chat", response_path="reply")
        session = await HttpChatTarget(config).open(context())
        replies = [
            "".join(chunk.text for chunk in await turn(session, text))
            for text in ("I want to cancel my visit", "C-1188 75005", "yes")
        ]
        await session.close()
    assert "customer number" in replies[0]
    assert replies[1].startswith("Shall I cancel your boiler service visit")
    assert "is cancelled" in replies[2]


@pytest.mark.parametrize("via", ["world_url_header", "world_base_url"])
async def test_tools_go_to_the_trial_sandbox(
    world: World, personas: dict[str, Persona], via: str
) -> None:
    """The adapter's world URL header (what ``hecklr run`` uses) or the base URL setting."""
    trial_id = TrialId(new_id())
    scenario = Scenario.model_validate(
        {
            "id": "cancel",
            "title": "Cancel a visit",
            "channels": ["chat"],
            "persona": "polite-professional",
            "world": "acme-maintenance",
            "goal": "Cancel the visit",
            "success": [{"type": "tool_called", "tool": "cancel_appointment"}],
        }
    )
    brief = TrialBrief(
        trial_id=trial_id,
        scenario=scenario,
        persona=personas["polite-professional"],
        channel=Channel.CHAT,
        seed=7,
    )
    sink = ListSink(trial_id)
    async with Sandbox() as sandbox:
        trial_world = await sandbox.open(brief, world, sink)
        by_header = via == "world_url_header"
        app = rules_app(world, world_base_url=None if by_header else sandbox.base_url)
        ctx = SessionContext(
            trial_id=trial_id, seed=7, world_http_url=trial_world.http_url if by_header else None
        )
        async with serving(app) as url, httpx2.AsyncClient() as client:
            config = http_config(f"{url}/chat/stream", stream="sse")
            session = await HttpChatTarget(config).open(ctx)
            chunks = [
                chunk
                for text in ("Please cancel my visit", "C-1188 75005", "yes")
                for chunk in await turn(session, text)
            ]
            await session.close()
            health = (await client.get(f"{url}/healthz")).json()
        after = trial_world.snapshot()
        await trial_world.close()
    assert after.collections["appointments"]["A-3003"]["status"] == "cancelled"
    # The sandbox logged the calls; the agent did not report them a second time.
    assert all(chunk.tool_call is None for chunk in chunks)
    tools = [e.payload.tool for e in sink.events if isinstance(e.payload, ToolCallPayload)]
    assert tools == ["verify_customer", "list_appointments", "get_policy", "cancel_appointment"]
    source = "header" if by_header else "base_url"
    assert health["requests_by_world_source"] == {
        "body": 0,
        "header": 0,
        "base_url": 0,
        "local": 0,
        source: 3,
    }


async def test_a_sandbox_agent_without_a_trial_id_is_a_client_error(world: World) -> None:
    async with (
        serving(rules_app(world, world_base_url="http://127.0.0.1:9")) as url,
        httpx2.AsyncClient() as client,
    ):
        response = await client.post(f"{url}/chat", json={"message": "hi", "session_id": "s"})
        assert response.status_code == 400
        assert TRIAL_ID_HEADER in response.text


async def test_an_unreachable_sandbox_becomes_an_apology(world: World) -> None:
    seen: list[httpx2.Request] = []
    trial_id = new_id()
    app = rules_app(world, world_transport=refusing_transport(seen))
    async with serving(app) as url, httpx2.AsyncClient() as client:
        response = await client.post(
            f"{url}/chat",
            json={"message": "I smell gas", "session_id": "s"},
            headers={
                TRIAL_ID_HEADER: trial_id,
                WORLD_URL_HEADER: f"http://sandbox.test/t/{trial_id}",
            },
        )
    assert response.status_code == 200
    assert "couldn't open a ticket" in response.json()["reply"]
    [request] = seen
    assert str(request.url) == f"http://sandbox.test/t/{trial_id}/tools/create_emergency_ticket"


async def test_request_world_url_and_health(world: World) -> None:
    async with serving(rules_app(world)) as url, httpx2.AsyncClient() as client:
        before = (await client.get(f"{url}/healthz")).json()
        response = await client.post(
            f"{url}/chat",
            json={
                "message": "Book a visit",
                "session_id": "s",
                "world_url": "http://127.0.0.1:9/t/x",
            },
            # The body wins over the header.
            headers={WORLD_URL_HEADER: "http://127.0.0.1:9/t/y"},
        )
        # An empty body URL (a ``{{world_url}}`` template without a sandbox) is no URL.
        await client.post(
            f"{url}/chat", json={"message": "Book a visit", "session_id": "t", "world_url": ""}
        )
        after = (await client.get(f"{url}/healthz")).json()
    assert before == {
        "status": "ok",
        "brain": "rules",
        "variant": "clean",
        "model": None,
        "fallback_tools": "local",
        "requests_by_world_source": {"body": 0, "header": 0, "base_url": 0, "local": 0},
        "spent_usd": "0",
        "failed_turns": {},
    }
    assert response.json()["tool_calls"] == []
    assert after["requests_by_world_source"] == {"body": 1, "header": 0, "base_url": 0, "local": 1}


def test_multi_line_text_becomes_several_data_lines() -> None:
    assert sse_frame(None, "a\nb") == "data: a\ndata: b\n\n"
    assert sse_frame("end", "") == "event: end\ndata: \n\n"


async def test_in_process_agent_uses_the_sessions_sandbox(
    world: World, personas: dict[str, Persona]
) -> None:
    trial_id = TrialId(new_id())
    scenario = Scenario.model_validate(
        {
            "id": "gas",
            "title": "Gas smell",
            "channels": ["chat"],
            "persona": "busy-homeowner",
            "world": "acme-maintenance",
            "goal": "Report a gas smell",
            "success": [{"type": "tool_called", "tool": "create_emergency_ticket"}],
        }
    )
    brief = TrialBrief(
        trial_id=trial_id,
        scenario=scenario,
        persona=personas["busy-homeowner"],
        channel=Channel.CHAT,
        seed=7,
    )
    sink = ListSink(trial_id)
    agent = AcmeAgent(load_agent_files(), AgentSettings(brain="rules"))
    async with Sandbox() as sandbox, InProcessAgent(agent, world) as chat:
        trial_world = await sandbox.open(brief, world, sink)
        ctx = SessionContext(trial_id=trial_id, seed=7, world_http_url=trial_world.http_url)
        chunks = [chunk async for chunk in chat(AgentSession(id="a", context=ctx), "I smell gas")]
        alone = AgentSession(id="b", context=context())
        local = [chunk async for chunk in chat(alone, "I smell gas")]
        await trial_world.close()
    assert any(chunk.handoff is not None for chunk in chunks)
    assert all(chunk.tool_call is None for chunk in chunks)  # the sandbox logged it
    tools = [e.payload.tool for e in sink.events if isinstance(e.payload, ToolCallPayload)]
    assert tools == ["create_emergency_ticket"]
    assert any(chunk.tool_call is not None for chunk in local)  # nobody else saw these
