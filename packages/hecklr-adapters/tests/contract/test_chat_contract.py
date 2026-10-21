"""The chat adapter contract: every chat adapter must pass this suite against its reference agent.

Covers connect, multi-turn memory, streaming shape, trial-id propagation, session isolation,
timeouts and errors (spec "Agent adapters", WS6).
"""

import time
from typing import Any

import anyio
import pytest

from hecklr_adapters import AgentError, AgentTimeoutError
from hecklr_schemas import AgentChunk, ChatSession, ChatTarget, SessionContext, TrialId, new_id

from .conftest import ERROR, SLOW, Case, reply_text

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.enable_socket,
    pytest.mark.allow_hosts(["127.0.0.1"]),
]


def _ctx(**fields: Any) -> SessionContext:
    return SessionContext(trial_id=TrialId(new_id()), seed=7, **fields)


async def _turn(session: ChatSession, text: str) -> list[AgentChunk]:
    return [chunk async for chunk in session.send(text)]


def _text(chunks: list[AgentChunk]) -> str:
    return "".join(chunk.text for chunk in chunks)


async def test_ws6_contract_target_satisfies_the_protocol_and_opens_and_closes(case: Case) -> None:
    assert isinstance(case.target, ChatTarget)
    session = await case.target.open(_ctx())
    assert isinstance(session, ChatSession)
    summary = await session.close()
    assert summary.turns == 0


async def test_ws6_contract_multi_turn_conversation_keeps_memory(case: Case) -> None:
    session = await case.target.open(_ctx())
    first = _text(await _turn(session, "hello there"))
    second = _text(await _turn(session, "and again"))
    summary = await session.close()

    assert first == reply_text(["hello there"])
    assert second == reply_text(["hello there", "and again"])
    assert summary.turns == 2


async def test_ws6_contract_reply_streams_and_ends_with_exactly_one_final_chunk(
    case: Case,
) -> None:
    session = await case.target.open(_ctx())
    chunks = await _turn(session, "stream this reply please")
    await session.close()

    assert chunks[-1].final
    assert sum(chunk.final for chunk in chunks) == 1
    text_chunks = [chunk for chunk in chunks if chunk.text]
    assert _text(chunks) == reply_text(["stream this reply please"])
    if case.streams:
        assert len(text_chunks) > 1
    else:
        assert len(text_chunks) == 1


async def test_ws6_contract_every_message_carries_the_trial_id(case: Case) -> None:
    ctx = _ctx()
    session = await case.target.open(ctx)
    await _turn(session, "one")
    await _turn(session, "two")
    await session.close()
    assert case.agent.trial_ids == [ctx.trial_id, ctx.trial_id]


async def test_ws6_contract_concurrent_sessions_do_not_share_memory(case: Case) -> None:
    left = await case.target.open(_ctx())
    right = await case.target.open(_ctx())
    await _turn(left, "left first")
    await _turn(right, "right first")
    assert _text(await _turn(left, "left second")) == reply_text(["left first", "left second"])
    assert _text(await _turn(right, "right second")) == reply_text(["right first", "right second"])
    await left.close()
    await right.close()


async def test_ws6_contract_agent_failure_raises_agent_error(case: Case) -> None:
    session = await case.target.open(_ctx())
    with pytest.raises(AgentError):
        await _turn(session, ERROR)
    await session.close()


async def test_ws6_contract_a_stalled_reply_can_be_cancelled_and_closed(case: Case) -> None:
    """The driver's per-turn deadline cancels ``send``; the session must still close promptly."""
    session = await case.target.open(_ctx())
    started = time.monotonic()
    with anyio.move_on_after(0.2) as scope:
        await _turn(session, SLOW)
    with anyio.fail_after(2):
        await session.close()
    assert scope.cancelled_caught
    assert time.monotonic() - started < 2


async def test_ws6_contract_network_timeout_raises_agent_timeout_error(case: Case) -> None:
    if case.timeout_s is None:
        pytest.skip("in-process agents have no transport timeout; the driver's deadline applies")
    session = await case.target.open(_ctx())
    started = time.monotonic()
    with pytest.raises(AgentTimeoutError):
        await _turn(session, SLOW)
    assert time.monotonic() - started < case.timeout_s + 1
    await session.close()
