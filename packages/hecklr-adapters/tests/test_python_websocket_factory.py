"""Edge cases of the ``python`` and ``websocket`` adapters, and ``make_chat_target``."""

import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import TypeAdapter
from websockets.asyncio.client import ClientConnection

from hecklr_adapters import (
    AdapterConfigError,
    AgentError,
    AgentSession,
    HttpChatTarget,
    OpenAICompatibleChatTarget,
    PythonChatTarget,
    WebSocketChatSession,
    WebSocketChatTarget,
    load_agent,
    make_chat_target,
)
from hecklr_schemas import (
    AgentChunk,
    ChatAdapterConfig,
    HandoffPayload,
    SessionContext,
    ToolCallPayload,
    TrialId,
    WebSocketAdapterConfig,
    new_id,
)

CTX = SessionContext(trial_id=TrialId(new_id()), seed=1)


async def _reply(target: PythonChatTarget, text: str = "hi") -> list[AgentChunk]:
    session = await target.open(CTX)
    chunks = [chunk async for chunk in session.send(text)]
    await session.close()
    return chunks


# --- python ------------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_python_agent_streams_tool_events_and_a_single_final_chunk() -> None:
    call = ToolCallPayload(call_id="c1", tool="book")

    async def agent(session: AgentSession, message: str) -> AsyncIterator[str | AgentChunk]:
        yield "booking "
        yield AgentChunk(tool_call=call)
        yield AgentChunk(handoff=HandoffPayload(reason="human"), ended=True, final=True)
        yield AgentChunk(final=True)

    chunks = await _reply(PythonChatTarget(agent))
    assert chunks == [
        AgentChunk(text="booking "),
        AgentChunk(tool_call=call),
        AgentChunk(handoff=HandoffPayload(reason="human"), ended=True),
        AgentChunk(final=True),
    ]


@pytest.mark.anyio
async def test_python_agent_session_exposes_a_stable_id_and_the_context() -> None:
    seen: list[AgentSession] = []

    async def agent(session: AgentSession, message: str) -> str:
        seen.append(session)
        return "ok"

    session = await PythonChatTarget(agent).open(CTX)
    for text in ("a", "b"):
        _ = [chunk async for chunk in session.send(text)]
    summary = await session.close()
    assert seen[0] == seen[1]
    assert seen[0].context == CTX
    assert summary.agent_session_id == seen[0].id
    assert summary.turns == 2


@pytest.mark.anyio
async def test_python_agent_exceptions_become_agent_errors_with_the_cause() -> None:
    async def agent(session: AgentSession, message: str) -> str:
        raise KeyError("customer")

    with pytest.raises(AgentError, match="agent raised KeyError") as caught:
        await _reply(PythonChatTarget(agent))
    assert isinstance(caught.value.__cause__, KeyError)


@pytest.mark.anyio
async def test_python_agent_raising_agent_error_is_passed_through() -> None:
    async def agent(session: AgentSession, message: str) -> str:
        raise AgentError("upstream down")

    with pytest.raises(AgentError, match=r"^upstream down$"):
        await _reply(PythonChatTarget(agent))


@pytest.mark.anyio
async def test_python_agent_yielding_the_wrong_type_is_an_agent_error() -> None:
    async def agent(session: AgentSession, message: str) -> AsyncIterator[str | AgentChunk]:
        yield cast(str, 42)

    with pytest.raises(AgentError, match="produced int"):
        await _reply(PythonChatTarget(agent))


def test_load_agent_imports_an_entrypoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = tmp_path / "ws6_entry_agent.py"
    module.write_text("async def reply(session, message):\n    return message\nNOT_CALLABLE = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert callable(load_agent("ws6_entry_agent:reply"))
    with pytest.raises(AdapterConfigError, match="not a callable"):
        load_agent("ws6_entry_agent:NOT_CALLABLE")
    with pytest.raises(AdapterConfigError, match="not a callable"):
        load_agent("ws6_entry_agent:missing")
    with pytest.raises(AdapterConfigError, match="cannot import"):
        load_agent("ws6_no_such_module:reply")


# --- websocket ---------------------------------------------------------------------------------


class ScriptedSocket:
    """Stands in for a ``ClientConnection``: replays canned incoming messages."""

    def __init__(self, incoming: list[str]) -> None:
        self.incoming = incoming
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self, decode: bool) -> str:
        return self.incoming.pop(0)


def _ws_session(incoming: list[str], **fields: Any) -> WebSocketChatSession:
    config = WebSocketAdapterConfig.model_validate(
        {"type": "websocket", "url": "ws://agent.test/ws", **fields}
    )
    socket = cast(ClientConnection, ScriptedSocket(incoming))
    return WebSocketChatSession(config, {"m": "{{message}}"}, CTX, socket)


@pytest.mark.anyio
async def test_websocket_end_marker_without_text_ends_the_turn() -> None:
    session = _ws_session(
        ['{"t": "a"}', '{"t": "b", "end": false}', '{"end": true}'],
        receive_path="t",
        final_path="end",
    )
    chunks = [chunk async for chunk in session.send("hi")]
    assert [chunk.text for chunk in chunks] == ["a", "b", ""]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("message", "match"),
    [('{"t": "a", "end": "yes"}', "not a boolean"), ('{"t": 3, "end": true}', "not text")],
)
async def test_websocket_malformed_messages_are_agent_errors(message: str, match: str) -> None:
    session = _ws_session([message], receive_path="t", final_path="end")
    with pytest.raises(AgentError, match=match):
        _ = [chunk async for chunk in session.send("hi")]


def test_websocket_final_path_needs_receive_path() -> None:
    config = WebSocketAdapterConfig(type="websocket", url="ws://agent.test", final_path="done")
    with pytest.raises(AdapterConfigError, match="needs receive_path"):
        WebSocketChatTarget(config)


@pytest.mark.anyio
@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_websocket_unreachable_agent_is_an_agent_error() -> None:
    with socket.socket() as probe:  # a port nobody listens on once the probe closes
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    config = WebSocketAdapterConfig(type="websocket", url=f"ws://127.0.0.1:{port}/ws")
    with pytest.raises(AgentError, match="cannot connect"):
        await WebSocketChatTarget(config).open(CTX)


# --- factory -----------------------------------------------------------------------------------


def _config(data: dict[str, Any]) -> ChatAdapterConfig:
    return TypeAdapter[ChatAdapterConfig](ChatAdapterConfig).validate_python(data)


@pytest.mark.parametrize(
    ("data", "kind"),
    [
        ({"type": "http", "url": "http://a.test/chat"}, HttpChatTarget),
        ({"type": "openai-compatible", "url": "http://a.test/v1"}, OpenAICompatibleChatTarget),
        ({"type": "websocket", "url": "ws://a.test/ws"}, WebSocketChatTarget),
        ({"type": "python", "entrypoint": "hecklr_adapters.templates:render"}, PythonChatTarget),
    ],
)
def test_make_chat_target_builds_the_configured_adapter(data: dict[str, Any], kind: type) -> None:
    assert isinstance(make_chat_target(_config(data)), kind)


def test_make_chat_target_refuses_the_webhook_adapter_for_now() -> None:
    with pytest.raises(AdapterConfigError, match="webhook"):
        make_chat_target(_config({"type": "webhook", "url": "http://a.test/hook"}))
