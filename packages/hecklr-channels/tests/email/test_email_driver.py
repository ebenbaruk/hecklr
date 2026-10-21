"""EmailDriver on a virtual clock with in-process agents: threading, timing, limits, failures."""

from collections.abc import AsyncIterator, Sequence

import anyio
import pytest

from hecklr_adapters import AgentError, AgentSession, AgentTimeoutError
from hecklr_adapters.email import PythonEmailAgent, PythonEmailTarget, make_reply
from hecklr_channels import EmailDriver
from hecklr_channels.email import QuoteStyle
from hecklr_schemas import (
    Channel,
    ChannelDriver,
    DialogueTurn,
    EmailAttachment,
    EmailMessage,
    EndReason,
    ErrorPayload,
    FakeClock,
    MonotonicClock,
    SessionContext,
    SimEnd,
    SimTurn,
)

from ..fakes import ListSink, ScriptedCustomer
from .support import (
    AGENT,
    SEVEN_MINUTES_MS,
    ThreadedAgent,
    email_brief,
    email_context,
    emails,
    grade_threading,
    threaded_reply,
)

pytestmark = pytest.mark.anyio


def _driver(
    agent: PythonEmailAgent,
    *,
    reply_timeout_s: float | None = None,
    quote_style: QuoteStyle | None = None,
) -> EmailDriver:
    return EmailDriver(
        PythonEmailTarget(agent),
        agent_addr=AGENT,
        reply_timeout_s=reply_timeout_s,
        quote_style=quote_style,
    )


def _errors(sink: ListSink) -> list[str]:
    return [e.payload.message for e in sink.events if isinstance(e.payload, ErrorPayload)]


def test_ws12_email_driver_satisfies_the_channel_driver_protocol() -> None:
    driver = _driver(ThreadedAgent())
    assert isinstance(driver, ChannelDriver)
    assert driver.channel is Channel.EMAIL


async def test_ws12_email_reply_time_is_measured_on_the_simulated_clock() -> None:
    clock = FakeClock(start_ms=1_000)
    sink = ListSink(clock)
    agent = ThreadedAgent(clock, delay_ms=SEVEN_MINUTES_MS)
    customer = ScriptedCustomer(["My boiler visit needs moving", "Tuesday works"])

    result = await _driver(agent).run(email_context(), email_brief(), customer, sink)

    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.GOAL_ACHIEVED, 4)
    assert sink.kinds() == ["user:email", "agent:email"] * 2
    [(sent, _), (_, _)] = emails(sink.events, "user")
    [(replied, _), _] = emails(sink.events, "agent")
    assert sent.t_ms == 1_000
    assert replied.t_ms - sent.t_ms == SEVEN_MINUTES_MS  # reply time, in minutes, on the clock


async def test_ws12_customer_emails_thread_like_a_mail_client() -> None:
    sink = ListSink(FakeClock())
    agent = ThreadedAgent()
    customer = ScriptedCustomer(["Can we move the visit?", "Tuesday morning then"])

    await _driver(agent, quote_style="top_post").run(email_context(), email_brief(), customer, sink)

    [(_, first), (_, second)] = emails(sink.events, "user")
    [(_, reply), _] = emails(sink.events, "agent")
    assert first.subject == "Move a visit"  # the scenario title when the simulator gives none
    assert (first.in_reply_to, first.references, first.to_addrs) == (None, [], [AGENT])
    assert second.in_reply_to == reply.message_id
    assert second.references == [first.message_id, reply.message_id]
    assert second.subject == "Re: Move a visit"
    assert second.from_addr == first.from_addr
    assert second.from_addr.endswith("@customers.hecklr.test")
    assert second.body.startswith("Tuesday morning then\n\nOn a previous message")
    assert f"> {reply.body}" in second.body
    assert agent.received == [first, second]


async def test_ws12_clean_agent_passes_the_threading_grader() -> None:
    sink = ListSink(FakeClock())
    brief = email_brief()
    customer = ScriptedCustomer(["Can we move the visit?", "Tuesday", "Morning please"])

    await _driver(ThreadedAgent()).run(email_context(), brief, customer, sink)

    grade = await grade_threading(sink.events, brief)
    assert (grade.status, grade.passed) == ("ok", True), grade.rationale


async def test_sc8_f11_broken_threading_agent_is_caught() -> None:
    sink = ListSink(FakeClock())
    brief = email_brief()
    customer = ScriptedCustomer(["Can we move the visit?", "Tuesday"])

    result = await _driver(ThreadedAgent(broken=True)).run(email_context(), brief, customer, sink)

    assert result.status == "ok"  # losing the thread is a quality failure, not a crash
    [(_, reply), _] = emails(sink.events, "agent")
    assert (reply.in_reply_to, reply.references) == (None, [])  # recorded exactly as sent
    grade = await grade_threading(sink.events, brief)
    assert (grade.status, grade.passed) == ("ok", False)
    assert "no In-Reply-To" in (grade.rationale or "")


async def test_ws12_the_simulator_reads_new_text_and_attachments_not_quoted_history() -> None:
    pdf = EmailAttachment(filename="invoice.pdf", content_type="application/pdf", data=b"%PDF")

    async def quoting_agent(session: AgentSession, msg: EmailMessage) -> EmailMessage:
        body = f"Here is your invoice.\n\nOn Monday, {msg.from_addr} wrote:\n> {msg.body}"
        return make_reply(msg, body=body, from_addr=AGENT, attachments=[pdf])

    sink = ListSink(FakeClock())
    customer = ScriptedCustomer(["Send me the invoice"])
    await _driver(quoting_agent).run(email_context(), email_brief(), customer, sink)

    [(_, reply)] = emails(sink.events, "agent")
    assert reply.attachments == [pdf]  # recorded with the event
    [_, seen_reply] = customer.seen[-1]
    assert seen_reply.text == "Here is your invoice.\n[attachment: invoice.pdf (application/pdf)]"


async def test_ws12_every_reply_is_logged_when_the_agent_sends_two() -> None:
    async def ack_then_answer(session: AgentSession, msg: EmailMessage) -> list[EmailMessage]:
        ack = make_reply(msg, body="We got your email.", from_addr=AGENT)
        return [ack, threaded_reply(msg)]

    sink = ListSink(FakeClock())
    customer = ScriptedCustomer(["Hello", "Tuesday"])
    result = await _driver(ack_then_answer).run(email_context(), email_brief(), customer, sink)

    assert sink.kinds() == ["user:email", "agent:email", "agent:email"] * 2
    assert result.turns == 6
    [_, (_, second)] = emails(sink.events, "user")
    [_, (_, answer), _, _] = emails(sink.events, "agent")
    assert second.in_reply_to == answer.message_id  # the customer answers the latest email


async def test_ws12_a_silent_agent_times_out() -> None:
    async def silent(session: AgentSession, msg: EmailMessage) -> None:
        return None

    sink = ListSink(MonotonicClock())
    driver = _driver(silent, reply_timeout_s=0.2)
    result = await driver.run(email_context(), email_brief(), ScriptedCustomer(["hi"]), sink)

    assert (result.status, result.end_reason) == ("timeout", EndReason.ERROR)
    assert result.error == "agent did not reply within 0.2 s"
    assert _errors(sink) == [result.error]
    assert sink.kinds() == ["user:email", "system:error"]


async def test_ws12_the_context_timeout_is_the_default_reply_deadline() -> None:
    async def silent(session: AgentSession, msg: EmailMessage) -> None:
        return None

    sink = ListSink(MonotonicClock())
    result = await _driver(silent).run(
        email_context(timeout_s=0.1), email_brief(), ScriptedCustomer(["hi"]), sink
    )
    assert result.error == "agent did not reply within 0.1 s"


async def test_ws12_running_out_of_scenario_time_while_waiting_is_max_duration() -> None:
    async def silent(session: AgentSession, msg: EmailMessage) -> None:
        return None

    sink = ListSink(MonotonicClock())
    result = await _driver(silent, reply_timeout_s=30).run(
        email_context(), email_brief(max_duration_s=1), ScriptedCustomer(["hi"]), sink
    )
    assert (result.status, result.end_reason) == ("timeout", EndReason.MAX_DURATION)


async def test_ws12_a_reply_after_the_scenario_deadline_is_logged_then_a_timeout() -> None:
    clock = FakeClock()
    sink = ListSink(clock)
    agent = ThreadedAgent(clock, delay_ms=61_000)
    result = await _driver(agent).run(
        email_context(), email_brief(max_duration_s=60), ScriptedCustomer(["hi", "more"]), sink
    )
    assert (result.status, result.end_reason) == ("timeout", EndReason.MAX_DURATION)
    assert sink.kinds() == ["user:email", "agent:email", "system:error"]


async def test_ws12_max_turns_counts_every_email_and_never_strands_a_message() -> None:
    sink = ListSink(FakeClock())
    customer = ScriptedCustomer(["one", "two", "three"])
    result = await _driver(ThreadedAgent()).run(
        email_context(), email_brief(max_turns=5), customer, sink
    )
    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.MAX_TURNS, 4)


async def test_ws12_max_duration_between_emails_ends_normally() -> None:
    clock = FakeClock()
    sink = ListSink(clock)
    agent = ThreadedAgent(clock, delay_ms=60_000)
    result = await _driver(agent).run(
        email_context(), email_brief(max_duration_s=60), ScriptedCustomer(["one", "two"]), sink
    )
    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.MAX_DURATION, 2)


async def test_ws12_closing_text_is_sent_without_waiting_for_a_reply() -> None:
    agent = ThreadedAgent()
    sink = ListSink(FakeClock())
    end = SimEnd(reason=EndReason.GOAL_ACHIEVED, closing_text="Great, thanks!")
    driver = _driver(agent, reply_timeout_s=30)
    with anyio.fail_after(5):  # waiting for an answer would take the full 30 s
        result = await driver.run(
            email_context(), email_brief(), ScriptedCustomer(["hi"], end), sink
        )
    assert (result.status, result.end_reason) == ("ok", EndReason.GOAL_ACHIEVED)
    [_, (_, goodbye)] = emails(sink.events, "user")
    assert goodbye.body.startswith("Great, thanks!")
    assert len(agent.received) == 2


async def test_ws12_closing_text_that_does_not_fit_is_dropped() -> None:
    sink = ListSink(FakeClock())
    end = SimEnd(reason=EndReason.GOAL_ABANDONED, closing_text="bye")
    result = await _driver(ThreadedAgent()).run(
        email_context(), email_brief(max_turns=2), ScriptedCustomer(["hi"], end), sink
    )
    assert (result.end_reason, result.turns) == (EndReason.MAX_TURNS, 2)


async def test_ws12_an_agent_that_raises_is_an_agent_error() -> None:
    async def crashing(session: AgentSession, msg: EmailMessage) -> None:
        raise RuntimeError("inbox exploded")

    sink = ListSink(FakeClock())
    result = await _driver(crashing).run(
        email_context(), email_brief(), ScriptedCustomer(["hi"]), sink
    )
    assert (result.status, result.end_reason) == ("agent_error", EndReason.ERROR)
    assert result.error == "agent raised RuntimeError: inbox exploded"
    assert sink.kinds() == ["user:email", "system:error"]


async def test_ws12_an_agent_timeout_error_is_a_timeout() -> None:
    async def stalled(session: AgentSession, msg: EmailMessage) -> None:
        raise AgentTimeoutError("mail server sent nothing")

    result = await _driver(stalled).run(
        email_context(), email_brief(), ScriptedCustomer(["hi"]), ListSink(FakeClock())
    )
    assert (result.status, result.error) == ("timeout", "mail server sent nothing")


async def test_ws12_closing_text_delivery_failure_is_reported() -> None:
    calls: list[str] = []

    async def fails_second(session: AgentSession, msg: EmailMessage) -> EmailMessage:
        calls.append(msg.body)
        if len(calls) == 2:
            raise AgentError("mailbox full")
        return threaded_reply(msg)

    end = SimEnd(reason=EndReason.GOAL_ACHIEVED, closing_text="bye")
    result = await _driver(fails_second).run(
        email_context(), email_brief(), ScriptedCustomer(["hi"], end), ListSink(FakeClock())
    )
    assert (result.status, result.error) == ("agent_error", "mailbox full")


class StreamTarget:
    """An ``EmailTarget`` whose reply stream is scripted: messages, then maybe an exception."""

    def __init__(self, items: Sequence[EmailMessage | Exception | None]) -> None:
        self.items = list(items)
        self.delivered: list[EmailMessage] = []
        self.first_delivery = anyio.Event()
        self.closed = False

    async def deliver(self, msg: EmailMessage, ctx: SessionContext) -> None:
        self.delivered.append(msg)
        self.first_delivery.set()

    async def replies(self, ctx: SessionContext) -> AsyncIterator[EmailMessage]:
        try:
            await self.first_delivery.wait()
            for item in self.items:
                if item is None:  # a pause, so later items arrive after the first reply
                    await anyio.sleep(0.05)
                    continue
                if isinstance(item, Exception):
                    raise item
                yield item.model_copy(update={"to_addrs": [self.delivered[0].from_addr]})
            await anyio.sleep_forever()
        finally:
            self.closed = True


def _stranger_reply() -> EmailMessage:
    return EmailMessage(
        message_id="<x@acme.test>",
        subject="Re: Move a visit",
        body="hello",
        from_addr=AGENT,
        to_addrs=["someone@else.test"],
    )


async def test_ws12_a_failing_reply_stream_is_an_agent_error_and_the_stream_is_closed() -> None:
    target = StreamTarget([AgentError("IMAP login refused")])
    sink = ListSink(FakeClock())
    result = await EmailDriver(target, agent_addr=AGENT).run(
        email_context(), email_brief(), ScriptedCustomer(["hi"]), sink
    )
    assert (result.status, result.error) == ("agent_error", "IMAP login refused")
    assert target.closed


@pytest.mark.parametrize(
    ("pause", "delivered"),
    [
        pytest.param(False, 1, id="noticed-before-writing-again"),
        pytest.param(True, 2, id="noticed-while-waiting"),
    ],
)
async def test_ws12_a_stream_failure_after_a_reply_is_an_agent_error(
    pause: bool, delivered: int
) -> None:
    items = [_stranger_reply(), None, AgentError("connection reset")]
    target = StreamTarget(items if pause else [items[0], items[2]])
    customer = ScriptedCustomer(["hi", "again"])
    result = await EmailDriver(target, agent_addr=AGENT).run(
        email_context(), email_brief(), customer, ListSink(MonotonicClock())
    )
    assert (result.status, result.error) == ("agent_error", "connection reset")
    assert len(target.delivered) == delivered


async def test_ws12_a_harness_failure_in_the_reply_stream_propagates() -> None:
    target = StreamTarget([ValueError("adapter bug")])
    with pytest.raises(ValueError, match="adapter bug"):
        await EmailDriver(target, agent_addr=AGENT).run(
            email_context(), email_brief(), ScriptedCustomer(["hi"]), ListSink(FakeClock())
        )
    assert target.closed


async def test_ws12_a_simulator_failure_propagates_and_releases_the_mailbox() -> None:
    class BrokenCustomer(ScriptedCustomer):
        async def next_turn(self, dialogue: Sequence[DialogueTurn]) -> SimTurn | SimEnd:
            raise RuntimeError("simulator down")

    target = StreamTarget([])
    with pytest.raises(RuntimeError, match="simulator down"):
        await EmailDriver(target, agent_addr=AGENT).run(
            email_context(), email_brief(), BrokenCustomer([]), ListSink(FakeClock())
        )
    assert target.closed


async def test_ws12_mail_to_another_address_is_not_part_of_the_conversation() -> None:
    stale = _stranger_reply()

    class StaleFirst(StreamTarget):
        async def replies(self, ctx: SessionContext) -> AsyncIterator[EmailMessage]:
            yield stale  # e.g. a late reply to a failed attempt of this trial
            async for reply in super().replies(ctx):
                yield reply

    reply = _stranger_reply().model_copy(update={"message_id": "<real@acme.test>"})
    sink = ListSink(FakeClock())
    result = await EmailDriver(StaleFirst([reply]), agent_addr=AGENT).run(
        email_context(), email_brief(), ScriptedCustomer(["hi"]), sink
    )
    assert result.status == "ok"
    assert [m.message_id for _, m in emails(sink.events, "agent")] == ["<real@acme.test>"]


async def test_ws12_each_run_writes_from_a_fresh_address() -> None:
    agent = ThreadedAgent()
    driver = _driver(agent)
    for _ in range(2):
        await driver.run(
            email_context(), email_brief(), ScriptedCustomer(["hi"]), ListSink(FakeClock())
        )
    first, second = agent.received
    assert first.from_addr != second.from_addr
    assert first.from_addr.startswith("busy.")
    assert agent.sessions[0] != agent.sessions[1]  # a retried trial meets a fresh agent session


async def test_ws12_result_summary_names_both_addresses() -> None:
    result = await _driver(ThreadedAgent()).run(
        email_context(), email_brief(), ScriptedCustomer(["hi"]), ListSink(FakeClock())
    )
    assert result.summary is not None
    assert result.summary.metadata["agent_addr"] == AGENT
    assert result.summary.metadata["customer_addr"].startswith("busy.")
    assert result.summary.turns == 2
