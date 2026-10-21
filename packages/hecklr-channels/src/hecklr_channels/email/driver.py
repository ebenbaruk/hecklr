"""The email channel driver: a simulated customer and an email agent write to each other.

Each conversation writes from a fresh customer address (``<persona>.<ulid>@<customer_domain>``),
so replies are routed by recipient and a retried trial never sees a failed attempt's mail. The
customer's emails thread like a real mail client's (``In-Reply-To`` and ``References`` of the
email answered, ``Re:`` subject) and quote it in the persona's style; whatever the agent sends is
recorded verbatim, threading headers and attachments included, for the ``email_threading``
grader.

Timing (ADR 0012a), on the sink's clock: a customer email is stamped when it is handed to the
agent; an agent email when it arrives. Reply time is the difference. Waiting is in real time,
bounded by ``reply_timeout_s``; tests that need email latency in minutes run the sink on a fake
clock the test agent advances.

Limits follow ADR 0006a: each email is a turn, a customer email is written only if its reply fits
``max_turns``, and ``max_duration_s`` runs from the start of ``run``. A reply arriving after the
duration ran out is logged, then reported as a ``timeout`` with ``max_duration``.
"""

import math
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass
from typing import Final, Literal

import anyio
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from hecklr_adapters import AgentError, AgentTimeoutError
from hecklr_adapters.email import make_reply, new_message_id
from hecklr_channels.email.quoting import QuoteStyle, persona_quote_style, quote, visible_text
from hecklr_schemas import (
    Channel,
    CustomerSimulator,
    DialogueTurn,
    DriverResult,
    EmailMessage,
    EmailPayload,
    EmailTarget,
    EndReason,
    ErrorPayload,
    EventSink,
    SessionContext,
    SessionSummary,
    SimEnd,
    SimTurn,
    TrialBrief,
    new_id,
)

DEFAULT_CUSTOMER_DOMAIN: Final = "customers.hecklr.test"
_LOCAL_PART_PREFIX_CHARS: Final = 36
"""Persona id characters kept in the address; with the ULID the local part stays under 64."""

DriverStatus = Literal["ok", "agent_error", "timeout"]


@dataclass(frozen=True, slots=True)
class _Outcome:
    status: DriverStatus
    end_reason: EndReason
    error: str | None = None


@dataclass(frozen=True, slots=True)
class _Settings:
    target: EmailTarget
    agent_addr: str
    customer_domain: str
    reply_timeout_s: float | None
    quote_style: QuoteStyle | None


@dataclass(frozen=True, slots=True)
class _Arrival:
    message: EmailMessage
    t_ms: int


_Item = _Arrival | Exception
"""What the reply pump forwards: an email with its arrival time, or why the stream failed."""


class EmailDriver:
    """Runs email conversations against one ``EmailTarget``; safe to share across trials."""

    def __init__(
        self,
        target: EmailTarget,
        *,
        agent_addr: str,
        customer_domain: str = DEFAULT_CUSTOMER_DOMAIN,
        reply_timeout_s: float | None = None,
        quote_style: QuoteStyle | None = None,
    ) -> None:
        """Bind the driver to an agent.

        Args:
            target: the agent's email adapter.
            agent_addr: the agent's address, the ``To`` of every customer email.
            customer_domain: domain of the simulated customers' addresses.
            reply_timeout_s: real-time deadline for each reply; ``SessionContext.timeout_s`` if
                ``None``.
            quote_style: force one quoting style; by default each persona has its own.
        """
        self._settings = _Settings(
            target, agent_addr, customer_domain, reply_timeout_s, quote_style
        )

    @property
    def channel(self) -> Channel:
        """Always ``Channel.EMAIL``."""
        return Channel.EMAIL

    async def run(
        self,
        ctx: SessionContext,
        brief: TrialBrief,
        simulator: CustomerSimulator,
        sink: EventSink,
    ) -> DriverResult:
        """Run one email conversation to its end.

        Agent failures and timeouts are reported in the result (and logged as ``error`` events);
        simulator, sink and other harness failures propagate.
        """
        trial = _EmailTrial(self._settings, ctx, brief, simulator, sink)
        send, receive = anyio.create_memory_object_stream[_Item](max_buffer_size=math.inf)
        failure: Exception | None = None
        with send, receive:
            async with anyio.create_task_group() as tasks:
                replies = self._settings.target.replies(ctx)
                tasks.start_soon(_pump, replies, sink, send, ctx.timeout_s)
                try:
                    outcome = await trial.converse(receive)
                except Exception as exc:  # noqa: BLE001 -- re-raised below, unwrapped
                    failure = exc
                tasks.cancel_scope.cancel()
        if failure is not None:
            raise failure
        return trial.result(outcome)


async def _pump(
    stream: AsyncIterator[EmailMessage],
    sink: EventSink,
    send: MemoryObjectSendStream[_Item],
    close_timeout_s: float,
) -> None:
    """Forward replies, stamped on arrival, until cancelled; a stream failure is forwarded too.

    Finalizing the stream is shielded and bounded, so the adapter releases its mailbox even when
    the conversation is cancelled, but a stuck adapter cannot hang the trial.
    """
    try:
        async for message in stream:
            send.send_nowait(_Arrival(message, sink.now_ms()))
    except Exception as exc:  # noqa: BLE001 -- forwarded; the conversation decides whose fault
        send.send_nowait(exc)
    finally:
        if isinstance(stream, AsyncGenerator):
            with anyio.move_on_after(close_timeout_s, shield=True):
                await stream.aclose()


class _EmailTrial:
    """State of one email conversation; a fresh one per ``EmailDriver.run``."""

    def __init__(
        self,
        settings: _Settings,
        ctx: SessionContext,
        brief: TrialBrief,
        simulator: CustomerSimulator,
        sink: EventSink,
    ) -> None:
        self._settings = settings
        self._ctx = ctx
        self._brief = brief
        self._limits = brief.scenario.limits
        self._simulator = simulator
        self._sink = sink
        self._style = settings.quote_style or persona_quote_style(brief.persona.id)
        self._timeout_s = settings.reply_timeout_s or ctx.timeout_s
        local = f"{brief.persona.id[:_LOCAL_PART_PREFIX_CHARS]}.{new_id().lower()}"
        self._customer_addr = f"{local}@{settings.customer_domain}"
        self._deadline_ms = sink.now_ms() + self._limits.max_duration_s * 1000
        self._thread: list[EmailMessage] = []
        self._dialogue: list[DialogueTurn] = []

    @property
    def turns(self) -> int:
        return len(self._dialogue)

    async def converse(self, replies: MemoryObjectReceiveStream[_Item]) -> _Outcome:
        """Alternate customer and agent emails until someone or something ends it."""
        while True:
            if (limit := self._limit_reached(reply_expected=True)) is not None:
                return _Outcome("ok", limit)
            move = await self._simulator.next_turn(tuple(self._dialogue))
            if isinstance(move, SimEnd):
                return await self._finish(move, replies)
            if (stop := await self._exchange(move, replies)) is not None:
                return stop

    def result(self, outcome: _Outcome) -> DriverResult:
        summary = SessionSummary(
            turns=self.turns,
            metadata={
                "customer_addr": self._customer_addr,
                "agent_addr": self._settings.agent_addr,
            },
        )
        return DriverResult(
            status=outcome.status,
            end_reason=outcome.end_reason,
            turns=self.turns,
            error=outcome.error,
            summary=summary,
        )

    # --- turns -------------------------------------------------------------------------------

    def _limit_reached(self, *, reply_expected: bool) -> EndReason | None:
        if self._sink.now_ms() >= self._deadline_ms:
            return EndReason.MAX_DURATION
        if self.turns + (2 if reply_expected else 1) > self._limits.max_turns:
            return EndReason.MAX_TURNS
        return None

    async def _finish(self, end: SimEnd, replies: MemoryObjectReceiveStream[_Item]) -> _Outcome:
        """Send the customer's goodbye, if any and if it fits; no reply is awaited for it.

        Waiting for an answer to "thanks, bye" would cost a full reply deadline whenever the
        agent (rightly) does not answer it; replies that already arrived are still logged.
        """
        if end.closing_text and self._limit_reached(reply_expected=False) is None:
            stop = await self._send(SimTurn(text=end.closing_text)) or self._log_arrived(replies)
            if stop is not None:
                return stop
        return _Outcome("ok", end.reason)

    async def _exchange(
        self, move: SimTurn, replies: MemoryObjectReceiveStream[_Item]
    ) -> _Outcome | None:
        """One customer email and the agent's reply; an outcome if the conversation must stop."""
        if (stop := self._log_arrived(replies)) is not None:
            return stop
        sent_ms = self._sink.now_ms()
        remaining_s = (self._deadline_ms - sent_ms) / 1000
        with anyio.move_on_after(min(self._timeout_s, remaining_s)) as scope:
            if (failed := await self._send(move)) is not None:
                return failed
            first = await self._next_reply(replies)
        if scope.cancelled_caught:
            return self._deadline_missed(remaining_s)
        if isinstance(first, _Outcome):
            return first
        self._log_reply(first)
        if first.t_ms > self._deadline_ms:
            limit = self._limits.max_duration_s
            message = f"scenario max_duration_s={limit} ran out before the reply arrived"
            return self._failed("timeout", message, EndReason.MAX_DURATION)
        return self._log_arrived(replies)

    async def _send(self, move: SimTurn) -> _Outcome | None:
        """Write the customer's email and hand it to the agent; an outcome if delivery failed."""
        message = self._compose(move)
        self._sink.emit("user", EmailPayload(message=message))
        self._thread.append(message)
        self._dialogue.append(DialogueTurn(speaker="user", text=move.text))
        try:
            await self._settings.target.deliver(message, self._ctx)
        except AgentError as exc:
            return self._agent_failed(exc)
        return None

    def _compose(self, move: SimTurn) -> EmailMessage:
        if not self._thread:
            return EmailMessage(
                message_id=new_message_id(self._settings.customer_domain),
                subject=move.subject or self._brief.scenario.title,
                body=move.text,
                from_addr=self._customer_addr,
                to_addrs=[self._settings.agent_addr],
            )
        # Every exchange ends on an agent email, so the customer always answers the agent.
        parent = self._thread[-1]
        return make_reply(
            parent, body=quote(move.text, parent, self._style), from_addr=self._customer_addr
        )

    async def _next_reply(self, replies: MemoryObjectReceiveStream[_Item]) -> _Arrival | _Outcome:
        """The next email to this conversation's address, or how the reply stream failed."""
        while True:
            item = await replies.receive()
            if (arrival := self._accept(item)) is not None:
                return arrival

    def _log_arrived(self, replies: MemoryObjectReceiveStream[_Item]) -> _Outcome | None:
        """Log extra replies that arrived since the last wait (e.g. an ack, then the answer).

        Returns the failure outcome if the reply stream failed meanwhile.
        """
        while True:
            try:
                item = replies.receive_nowait()
            except anyio.WouldBlock:
                return None
            arrival = self._accept(item)
            if isinstance(arrival, _Outcome):
                return arrival
            if arrival is not None:
                self._log_reply(arrival)

    def _accept(self, item: _Item) -> _Arrival | _Outcome | None:
        """An email for this conversation, a failure outcome, or ``None`` to skip the item.

        Mail to other addresses (a failed attempt of this trial) is not part of this conversation.
        """
        if isinstance(item, AgentError):
            return self._agent_failed(item)
        if isinstance(item, Exception):
            raise item
        recipients = {addr.casefold() for addr in item.message.to_addrs}
        return item if self._customer_addr.casefold() in recipients else None

    def _log_reply(self, arrival: _Arrival) -> None:
        message = arrival.message
        self._sink.emit("agent", EmailPayload(message=message), t_ms=arrival.t_ms)
        self._thread.append(message)
        self._dialogue.append(DialogueTurn(speaker="agent", text=_as_read(message)))

    # --- failures ----------------------------------------------------------------------------

    def _deadline_missed(self, remaining_s: float) -> _Outcome:
        if remaining_s < self._timeout_s:
            limit = self._limits.max_duration_s
            message = f"scenario max_duration_s={limit} ran out awaiting a reply"
            return self._failed("timeout", message, EndReason.MAX_DURATION)
        return self._failed("timeout", f"agent did not reply within {self._timeout_s} s")

    def _agent_failed(self, exc: AgentError) -> _Outcome:
        if isinstance(exc, AgentTimeoutError):
            return self._failed("timeout", str(exc))
        return self._failed("agent_error", str(exc))

    def _failed(
        self, status: DriverStatus, message: str, end_reason: EndReason = EndReason.ERROR
    ) -> _Outcome:
        self._sink.emit("system", ErrorPayload(source="agent", message=message))
        return _Outcome(status, end_reason, message)


def _as_read(message: EmailMessage) -> str:
    """The reply as the customer reads it: the new text, then what is attached."""
    lines = [visible_text(message.body)]
    lines += [f"[attachment: {a.filename} ({a.content_type})]" for a in message.attachments]
    return "\n".join(line for line in lines if line)
