"""The in-process email adapter."""

import anyio
import pytest

from hecklr_adapters import AgentError, AgentSession
from hecklr_adapters.email import PythonEmailTarget, make_reply
from hecklr_schemas import EmailMessage, EmailTarget, SessionContext, TrialId, new_id

pytestmark = pytest.mark.anyio

AGENT = "support@acme.test"


def ctx() -> SessionContext:
    return SessionContext(trial_id=TrialId(new_id()), seed=1)


def email(sender: str = "c@customers.hecklr.test") -> EmailMessage:
    return EmailMessage(
        message_id=f"<{new_id()}@c.test>", subject="Hi", body="Hello", from_addr=sender,
        to_addrs=[AGENT],
    )  # fmt: skip


async def echo(session: AgentSession, msg: EmailMessage) -> EmailMessage:
    return make_reply(msg, body=f"session {session.id}", from_addr=AGENT)


async def test_ws12_python_email_target_streams_the_agent_replies() -> None:
    target = PythonEmailTarget(echo)
    assert isinstance(target, EmailTarget)
    context = ctx()
    replies = target.replies(context)
    await target.deliver(email(), context)
    await target.deliver(email(), context)
    with anyio.fail_after(1):
        first, second = await anext(replies), await anext(replies)
    assert first.body == second.body  # one session per customer address
    await replies.aclose()  # type: ignore[attr-defined]  # an async generator underneath


async def test_ws12_python_email_target_waits_for_a_late_reply() -> None:
    target = PythonEmailTarget(echo)
    context = ctx()

    async def deliver_later() -> None:
        await anyio.sleep(0.05)
        await target.deliver(email(), context)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(deliver_later)
        with anyio.fail_after(1):
            reply = await anext(target.replies(context))
    assert reply.subject == "Re: Hi"


async def test_ws12_python_email_target_accepts_several_or_no_replies() -> None:
    async def two_or_none(session: AgentSession, msg: EmailMessage) -> list[EmailMessage] | None:
        if msg.body == "quiet":
            return None
        return [make_reply(msg, body=b, from_addr=AGENT) for b in ("ack", "answer")]

    target = PythonEmailTarget(two_or_none)
    context = ctx()
    await target.deliver(email().model_copy(update={"body": "quiet"}), context)
    await target.deliver(email(), context)
    replies = target.replies(context)
    with anyio.fail_after(1):
        assert [(await anext(replies)).body for _ in range(2)] == ["ack", "answer"]


async def test_ws12_python_email_agent_failures_are_agent_errors() -> None:
    async def crashes(session: AgentSession, msg: EmailMessage) -> None:
        raise KeyError("customer")

    async def wrong_type(session: AgentSession, msg: EmailMessage) -> str:
        return "not an email"

    async def own_error(session: AgentSession, msg: EmailMessage) -> None:
        raise AgentError("down for maintenance")

    with pytest.raises(AgentError, match="agent raised KeyError"):
        await PythonEmailTarget(crashes).deliver(email(), ctx())
    with pytest.raises(AgentError, match="agent produced str"):
        await PythonEmailTarget(wrong_type).deliver(email(), ctx())  # type: ignore[arg-type]
    with pytest.raises(AgentError, match="down for maintenance"):
        await PythonEmailTarget(own_error).deliver(email(), ctx())
