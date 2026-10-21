from collections.abc import AsyncIterator
from typing import Any

import pytest

import hecklr
from hecklr_adapters import AgentSession, PythonChatTarget
from hecklr_schemas import TRIAL_ID_HEADER, AgentChunk, SessionContext, TrialId, new_id

CONTEXT = SessionContext(
    trial_id=TrialId(new_id()),
    seed=7,
    world_mcp_url="http://127.0.0.1:1/t/x/mcp",
    world_http_url="http://127.0.0.1:1/t/x",
)


@hecklr.agent(channel="chat")
async def echo(session: hecklr.Session, message: str) -> str:
    return f"{session.id}|{session.trial_id}|{session.world_http_url}|{message}"


@hecklr.agent()
async def streamer(session: hecklr.Session, message: str) -> AsyncIterator[str | AgentChunk]:
    yield "Hello "
    yield "there"
    yield AgentChunk(ended=True)


@pytest.mark.anyio
async def test_python_adapter_hands_the_agent_a_hecklr_session() -> None:
    session = await PythonChatTarget(echo).open(CONTEXT)

    chunks = [chunk async for chunk in session.send("hi")]

    summary = await session.close()
    assert chunks[-1].final
    session_id, trial_id, url, message = chunks[0].text.split("|")
    assert session_id == summary.agent_session_id
    assert (trial_id, url, message) == (CONTEXT.trial_id, CONTEXT.world_http_url, "hi")


@pytest.mark.anyio
async def test_streaming_agent_yields_text_and_chunks() -> None:
    session = await PythonChatTarget(streamer).open(CONTEXT)

    chunks = [chunk async for chunk in session.send("hi")]

    assert "".join(chunk.text for chunk in chunks) == "Hello there"
    assert any(chunk.ended for chunk in chunks)


@pytest.mark.anyio
async def test_decorated_agent_still_accepts_a_session_directly() -> None:
    session = hecklr.Session.from_context("s-1", CONTEXT)

    reply = await echo(session, "direct")  # type: ignore[misc]  # the union return is awaitable here

    assert reply.startswith("s-1|")
    assert session.headers == {TRIAL_ID_HEADER: CONTEXT.trial_id}
    assert session.seed == 7
    assert session.world_mcp_url == CONTEXT.world_mcp_url


@pytest.mark.anyio
async def test_adapter_session_is_converted() -> None:
    reply = await echo(AgentSession(id="s-2", context=CONTEXT), "x")  # type: ignore[misc]  # see above

    assert reply.startswith("s-2|")


def test_agent_rejects_channels_it_cannot_serve_in_process() -> None:
    channel: Any = "voice"
    with pytest.raises(ValueError, match="in-process agents serve"):
        hecklr.agent(channel=channel)


def test_agent_rejects_a_synchronous_function() -> None:
    def blocking(session: hecklr.Session, message: str) -> str:
        return message

    decorate: Any = hecklr.agent()
    with pytest.raises(TypeError, match="async function"):
        decorate(blocking)


def test_decorator_keeps_the_function_name_and_docstring() -> None:
    assert echo.__name__ == "echo"
