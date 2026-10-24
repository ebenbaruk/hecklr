"""``cap_reply_size``: one agent turn carries at most N bytes of text (issue #34, ADR 00hra)."""

from collections.abc import AsyncIterator, Sequence

import pytest

from hecklr_adapters import DEFAULT_MAX_REPLY_BYTES, AgentError, cap_reply_size
from hecklr_schemas import (
    AgentChunk,
    ChatSession,
    SessionContext,
    SessionSummary,
    ToolCallPayload,
    TrialId,
    new_id,
)

CTX = SessionContext(trial_id=TrialId(new_id()), seed=1)


class ScriptedSession:
    """Replies with the same chunks to every message; records whether each stream was closed."""

    def __init__(self, chunks: Sequence[AgentChunk]) -> None:
        self.chunks = chunks
        self.streams_closed = 0
        self.closed = False

    async def send(self, text: str) -> AsyncIterator[AgentChunk]:
        try:
            for chunk in self.chunks:
                yield chunk
        finally:
            self.streams_closed += 1

    async def close(self) -> SessionSummary:
        self.closed = True
        return SessionSummary(turns=1)


class ScriptedTarget:
    def __init__(self, session: ScriptedSession) -> None:
        self.session = session

    async def open(self, ctx: SessionContext) -> ChatSession:
        return self.session


async def _reply(session: ChatSession) -> tuple[list[AgentChunk], AgentError | None]:
    received: list[AgentChunk] = []
    try:
        async for chunk in session.send("hi"):
            received.append(chunk)
    except AgentError as exc:
        return received, exc
    return received, None


@pytest.mark.anyio
async def test_issue34_replies_within_the_cap_pass_through_unchanged() -> None:
    chunks = [
        AgentChunk(text="ab"),
        AgentChunk(tool_call=ToolCallPayload(call_id="c", tool="t", args={})),
        AgentChunk(text="cd", final=True),
    ]
    inner = ScriptedSession(chunks)
    session = await cap_reply_size(ScriptedTarget(inner), 4).open(CTX)

    received, error = await _reply(session)
    second, _ = await _reply(session)

    assert error is None
    assert received == chunks
    assert second == chunks, "the cap is per turn, not per session"
    assert await session.close() == SessionSummary(turns=1)
    assert inner.closed


@pytest.mark.anyio
async def test_issue34_a_reply_over_the_cap_keeps_its_head_and_fails_the_turn() -> None:
    inner = ScriptedSession([AgentChunk(text="abc"), AgentChunk(text="déf"), AgentChunk(text="g")])
    session = await cap_reply_size(ScriptedTarget(inner), 5).open(CTX)

    received, error = await _reply(session)

    # "é" is two bytes: 3 + 2 fit, the "é" would straddle the cap and is dropped whole.
    assert [chunk.text for chunk in received] == ["abc", "d"]
    assert error is not None
    assert "agent reply over 5 bytes" in str(error)
    assert inner.streams_closed == 1


@pytest.mark.anyio
async def test_issue34_a_cap_hit_on_a_chunk_boundary_yields_no_empty_chunk() -> None:
    inner = ScriptedSession([AgentChunk(text="abc"), AgentChunk(text="d")])
    session = await cap_reply_size(ScriptedTarget(inner), 3).open(CTX)

    received, error = await _reply(session)

    assert [chunk.text for chunk in received] == ["abc"]
    assert error is not None


@pytest.mark.anyio
async def test_issue34_closing_the_capped_stream_early_closes_the_inner_one() -> None:
    inner = ScriptedSession([AgentChunk(text="a"), AgentChunk(text="b", final=True)])
    session = await cap_reply_size(ScriptedTarget(inner)).open(CTX)

    stream = session.send("hi")
    async for _ in stream:
        break
    await stream.aclose()  # type: ignore[attr-defined]  # send() is an async generator

    assert inner.streams_closed == 1


def test_issue34_the_default_cap_is_256_kib_and_must_be_positive() -> None:
    assert DEFAULT_MAX_REPLY_BYTES == 262_144
    with pytest.raises(ValueError, match="max_bytes must be >= 1"):
        cap_reply_size(ScriptedTarget(ScriptedSession([])), 0)


@pytest.mark.anyio
async def test_issue112_a_lone_surrogate_fails_the_turn_as_the_agents() -> None:
    # json.loads('"Sure \\ud83d"') gives this: an emoji escape cut in half.
    session = ScriptedSession([AgentChunk(text="Sure, "), AgentChunk(text="Saturday \ud83d ok")])
    capped = await cap_reply_size(ScriptedTarget(session)).open(CTX)

    received, error = await _reply(capped)

    assert [chunk.text for chunk in received] == ["Sure, ", "Saturday "]
    assert error is not None
    assert "not valid Unicode (an unpaired surrogate) '\\ud83d'" in str(error)
    assert session.streams_closed == 1


@pytest.mark.anyio
async def test_issue112_a_lone_surrogate_in_a_tool_call_fails_the_turn() -> None:
    call = ToolCallPayload(call_id="c1", tool="book", args={"note": "\udc00"})
    session = ScriptedSession([AgentChunk(tool_call=call)])
    capped = await cap_reply_size(ScriptedTarget(session)).open(CTX)

    received, error = await _reply(capped)

    assert received == []
    assert error is not None
    assert "not valid Unicode" in str(error)
