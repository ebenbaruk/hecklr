"""The email front end: threading, quoted history, one conversation per sender, tool deadlines."""

import itertools
from collections.abc import Mapping

import anyio
import httpx2
import pytest
from acme_agent import HttpWorldTools, LocalWorldTools, ToolOutcome
from acme_email import SIGNATURE, DeadlineTools, compose_reply, route_world
from acme_email.tools import backend_for, lower_case_headers
from email_testing import AGENT_ADDR, email_desk
from pydantic import JsonValue

from hecklr_adapters import WORLD_URL_HEADER
from hecklr_adapters.email import make_reply
from hecklr_channels.email import quote
from hecklr_schemas import TRIAL_ID_HEADER, EmailMessage, World

pytestmark = pytest.mark.anyio

CUSTOMER = "camille.01J@customers.hecklr.test"
_MESSAGE_NUMBERS = itertools.count(1)


def customer_email(body: str, *, sender: str = CUSTOMER, subject: str = "My visit") -> EmailMessage:
    return EmailMessage(
        message_id=f"<customer-{next(_MESSAGE_NUMBERS)}@customers.hecklr.test>",
        subject=subject,
        body=body,
        from_addr=sender,
        to_addrs=[AGENT_ADDR],
    )


class HangingTools:
    """A backend whose calls never return, like a sandbox tool under a ``timeout`` fault."""

    reports_calls = False

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call(self, name: str, args: Mapping[str, JsonValue]) -> ToolOutcome:
        self.calls.append(name)
        await anyio.sleep_forever()
        raise AssertionError("unreachable")


def test_ws12_threaded_reply_names_its_parent_and_the_chain() -> None:
    first = customer_email("Hello")
    agent_reply = make_reply(first, body="Hi", from_addr=AGENT_ADDR)
    second = make_reply(agent_reply, body="Thanks", from_addr=CUSTOMER)
    reply = compose_reply(second, "Done.", from_addr=AGENT_ADDR, threaded=True)
    assert reply.in_reply_to == second.message_id
    assert reply.references == [first.message_id, agent_reply.message_id, second.message_id]
    assert reply.subject == "Re: My visit"
    assert (reply.from_addr, reply.to_addrs) == (AGENT_ADDR, [CUSTOMER])
    assert reply.body == f"Done.\n\n{SIGNATURE}"


def test_sc8_f11_reply_loses_the_thread_but_keeps_everything_else() -> None:
    parent = customer_email("Hello")
    threaded = compose_reply(parent, "Hi.", from_addr=AGENT_ADDR, threaded=True)
    broken = compose_reply(parent, "Hi.", from_addr=AGENT_ADDR, threaded=False)
    assert (broken.in_reply_to, broken.references) == (None, [])
    assert broken.model_dump(exclude={"message_id", "in_reply_to", "references"}) == (
        threaded.model_dump(exclude={"message_id", "in_reply_to", "references"})
    )


async def test_the_brain_reads_only_the_new_text_of_each_email(world: World) -> None:
    desk = email_desk()
    tools = LocalWorldTools(world)
    first = customer_email("I need to cancel my visit")
    asked = await desk.answer(first, lambda: tools)
    assert "customer number" in asked.body
    # Top-posted answer quoting the agent's question: the quote must not reach the brain.
    second = make_reply(asked, body=quote("C-1188 75005", asked, "top_post"), from_addr=CUSTOMER)
    confirm = await desk.answer(second, lambda: tools)
    assert "Shall I cancel your boiler service visit" in confirm.body
    assert confirm.in_reply_to == second.message_id


async def test_each_sender_is_its_own_conversation(world: World) -> None:
    desk = email_desk()
    await desk.answer(customer_email("I need to cancel my visit"), lambda: LocalWorldTools(world))
    other = customer_email("C-1188 75005", sender="someone.else@customers.hecklr.test")
    reply = await desk.answer(other, lambda: LocalWorldTools(world))
    assert "Shall I cancel" not in reply.body  # no cancellation was started by this sender
    assert reply.to_addrs == ["someone.else@customers.hecklr.test"]


async def test_an_email_with_only_quoted_text_is_read_by_its_subject(world: World) -> None:
    desk = email_desk()
    email = customer_email("> an old message", subject="Help, I can smell gas in my kitchen!")
    reply = await desk.answer(email, lambda: LocalWorldTools(world))
    assert "emergency" in reply.body.lower()


async def test_a_hung_tool_times_out_under_the_deadline() -> None:
    hanging = HangingTools()
    tools = DeadlineTools(hanging, timeout_s=0.05)
    with anyio.fail_after(2):
        outcome = await tools.call("get_policy", {"policy_id": "fees"})
    assert not outcome.ok
    assert outcome.error_text() == "timeout: get_policy did not answer within 0.05 s"
    assert tools.reports_calls is False


async def test_a_tool_that_timed_out_fails_fast_until_it_may_be_retried() -> None:
    # PR #30: waiting out the deadline on every retry made the clean LLM agent miss the reply
    # deadline, so a hung tool looked like F12.
    hanging = HangingTools()
    tools = DeadlineTools(hanging, timeout_s=0.05, retry_after_s=0.3)
    await tools.call("get_policy", {"policy_id": "fees"})
    # A zero deadline fails at the first await: this proves the call never waits, on any machine
    # (a 40 ms wall-clock bound could flake on a loaded runner, #67).
    with anyio.fail_after(0):
        outcome = await tools.call("get_policy", {"policy_id": "fees"})
    assert outcome.error_text() == "timeout: get_policy is down: it did not answer within 0.05 s"
    assert hanging.calls == ["get_policy"]  # not called again while down
    await tools.call("get_customer", {"customer_id": "C-1042"})  # other tools are still tried
    assert hanging.calls == ["get_policy", "get_customer"]
    await anyio.sleep(0.3)
    outcome = await tools.call("get_policy", {"policy_id": "fees"})
    assert outcome.error_text() == "timeout: get_policy did not answer within 0.05 s"
    assert hanging.calls == ["get_policy", "get_customer", "get_policy"]


async def test_sc8_f12_without_a_deadline_a_hung_tool_never_returns() -> None:
    tools = DeadlineTools(HangingTools(), timeout_s=None)
    with anyio.move_on_after(0.3) as scope:
        await tools.call("get_policy", {"policy_id": "fees"})
    assert scope.cancelled_caught


async def test_sc8_f12_the_desk_goes_silent_when_a_tool_hangs() -> None:
    clean, faulty = email_desk(), email_desk("F12")
    email = customer_email("Help, I can smell gas in my kitchen!")
    with anyio.fail_after(2):
        reply = await clean.answer(email, HangingTools)
    assert "emergency line" in reply.body
    with anyio.move_on_after(0.5) as scope:
        await faulty.answer(email, HangingTools)
    assert scope.cancelled_caught


def test_deadline_tools_reports_calls_of_a_local_world(world: World) -> None:
    assert DeadlineTools(LocalWorldTools(world), 1.0).reports_calls is True


@pytest.mark.parametrize(
    ("headers", "body_url", "body_trial", "base_url", "expected"),
    [
        ({}, "http://w/t/1", "T1", "http://base", ("body", "http://w/t/1", "T1")),
        (
            {WORLD_URL_HEADER: "http://h/t/2", TRIAL_ID_HEADER: "T2"},
            "",
            None,
            "http://base",
            ("header", "http://h/t/2", "T2"),
        ),
        (
            {TRIAL_ID_HEADER: "T3"},
            None,
            None,
            "http://base/",
            ("base_url", "http://base/t/T3", "T3"),
        ),
        ({}, None, "T4", "http://base", ("base_url", "http://base/t/T4", "T4")),
        ({}, None, "T5", None, ("local", None, "T5")),
        ({}, None, None, "http://base", ("local", None, None)),
    ],
)
def test_route_world_prefers_body_then_header_then_base_url(
    headers: dict[str, str],
    body_url: str | None,
    body_trial: str | None,
    base_url: str | None,
    expected: tuple[str, str | None, str | None],
) -> None:
    route = route_world(
        lower_case_headers(headers.items()),
        body_url=body_url,
        body_trial_id=body_trial,
        base_url=base_url,
    )
    assert (route.source, route.url, route.trial_id) == expected


def test_lower_case_headers_keeps_the_first_of_repeated_names() -> None:
    assert lower_case_headers([("X-A", "1"), ("x-a", "2"), ("B", 3)]) == {"x-a": "1", "b": "3"}


async def test_backend_for_a_route(world: World) -> None:
    async with httpx2.AsyncClient() as client:
        local = backend_for(route_world({}), client, world)
        remote = backend_for(route_world({}, body_url="http://w/t/1"), client, world)
    assert isinstance(local, LocalWorldTools)
    assert isinstance(remote, HttpWorldTools)
