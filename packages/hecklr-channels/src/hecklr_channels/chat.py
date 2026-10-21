"""The chat channel driver: alternates simulated customer turns and agent replies.

Timing follows ADR 0006, all on the sink's clock. A user utterance is stamped when the message is
sent (``t_ms == end_ms``). An agent utterance is logged once the reply completes, stamped
``t_ms`` = first text token, ``end_ms`` = last text token, ``latency_ms`` = first token minus the
end of the user turn; so time to first token is ``latency_ms`` and time to last token is
``latency_ms + end_ms - t_ms``. A reply with no text has no first token: it is logged when the
turn ends, with ``latency_ms`` unset.

Limits (ADR 0006a): ``max_turns`` counts both parties' turns and a user turn starts only if its
reply fits; ``max_duration_s`` runs from the start of ``run``. Reaching either between turns ends
the conversation normally; running out of time mid-reply, or missing the per-turn deadline
``SessionContext.timeout_s``, is a ``timeout``.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Literal

import anyio

from hecklr_adapters import AgentError, AgentTimeoutError
from hecklr_schemas import (
    AgentChunk,
    Channel,
    ChatSession,
    ChatTarget,
    CustomerSimulator,
    DialogueTurn,
    DriverResult,
    EndReason,
    ErrorPayload,
    EventSink,
    ScenarioLimits,
    SessionContext,
    SessionSummary,
    SimEnd,
    TrialBrief,
    UtterancePayload,
)
from hecklr_schemas.events import Party

DriverStatus = Literal["ok", "agent_error", "timeout"]
"""What a driver can report; harness failures are raised instead."""


@dataclass(frozen=True, slots=True)
class _Outcome:
    status: DriverStatus
    end_reason: EndReason
    error: str | None = None


@dataclass(slots=True)
class _Reply:
    """An agent reply as it streams in."""

    parts: list[str] = field(default_factory=list)
    first_ms: int | None = None
    last_ms: int | None = None
    handoff: bool = False
    ended: bool = False

    @property
    def text(self) -> str:
        return "".join(self.parts)


class ChatDriver:
    """Runs chat conversations against one ``ChatTarget``; safe to share across trials."""

    def __init__(self, target: ChatTarget) -> None:
        self._target = target

    @property
    def channel(self) -> Channel:
        """Always ``Channel.CHAT``."""
        return Channel.CHAT

    async def run(
        self,
        ctx: SessionContext,
        brief: TrialBrief,
        simulator: CustomerSimulator,
        sink: EventSink,
    ) -> DriverResult:
        """Run one conversation to its end.

        Agent failures and timeouts are reported in the result (and logged as ``error`` events);
        simulator and sink failures propagate as harness errors.
        """
        trial = _ChatTrial(ctx, brief.scenario.limits, simulator, sink)
        session = await trial.open(self._target)
        if isinstance(session, _Outcome):
            return trial.result(session, summary=None)
        try:
            outcome = await trial.converse(session)
        except BaseException as exc:
            await _close_after_failure(session, ctx.timeout_s, exc)
            raise
        return trial.result(outcome, summary=await trial.close(session))


class _ChatTrial:
    """State of one conversation; a fresh one per ``ChatDriver.run``."""

    def __init__(
        self,
        ctx: SessionContext,
        limits: ScenarioLimits,
        simulator: CustomerSimulator,
        sink: EventSink,
    ) -> None:
        self._ctx = ctx
        self._limits = limits
        self._simulator = simulator
        self._sink = sink
        self._deadline_ms = sink.now_ms() + limits.max_duration_s * 1000
        self._dialogue: list[DialogueTurn] = []

    @property
    def turns(self) -> int:
        return len(self._dialogue)

    async def open(self, target: ChatTarget) -> ChatSession | _Outcome:
        """Open the session, or say how the agent failed to."""
        with anyio.move_on_after(self._ctx.timeout_s):
            try:
                return await target.open(self._ctx)
            except AgentError as exc:
                return self._agent_failed(exc)
        return self._failed(
            "timeout", f"agent did not open a session within {self._ctx.timeout_s} s"
        )

    async def converse(self, session: ChatSession) -> _Outcome:
        """Alternate customer and agent turns until someone or something ends it."""
        while True:
            if (limit := self._limit_reached()) is not None:
                return _Outcome("ok", limit)
            move = await self._simulator.next_turn(tuple(self._dialogue))
            if isinstance(move, SimEnd):
                return await self._finish(session, move)
            if (stop := await self._exchange(session, move.text)) is not None:
                return stop

    async def close(self, session: ChatSession) -> SessionSummary | None:
        """Close the session; an agent failing to close is logged, not fatal to the trial."""
        with anyio.move_on_after(self._ctx.timeout_s):
            try:
                return await session.close()
            except AgentError as exc:
                self._log_error(f"closing the session failed: {exc}")
                return None
        self._log_error(f"agent did not close the session within {self._ctx.timeout_s} s")
        return None

    def result(self, outcome: _Outcome, summary: SessionSummary | None) -> DriverResult:
        return DriverResult(
            status=outcome.status,
            end_reason=outcome.end_reason,
            turns=self.turns,
            error=outcome.error,
            summary=summary,
        )

    # --- turns -------------------------------------------------------------------------------

    def _limit_reached(self) -> EndReason | None:
        if self._sink.now_ms() >= self._deadline_ms:
            return EndReason.MAX_DURATION
        if self.turns + 2 > self._limits.max_turns:
            return EndReason.MAX_TURNS
        return None

    async def _finish(self, session: ChatSession, end: SimEnd) -> _Outcome:
        """Send the customer's goodbye, if any and if it fits, then end with their reason."""
        if end.closing_text and self._limit_reached() is None:
            stop = await self._exchange(session, end.closing_text)
            if stop is not None and stop.status != "ok":
                return stop
        return _Outcome("ok", end.reason)

    async def _exchange(self, session: ChatSession, text: str) -> _Outcome | None:
        """One user message and the agent's reply; an outcome if the conversation must stop."""
        sent_ms = self._sink.now_ms()
        self._log_turn("user", text, t_ms=sent_ms, end_ms=sent_ms, latency_ms=None)
        reply = _Reply()
        remaining_s = (self._deadline_ms - sent_ms) / 1000
        try:
            with anyio.move_on_after(min(self._ctx.timeout_s, remaining_s)) as scope:
                await self._stream_reply(session.send(text), reply)
        except AgentError as exc:
            return self._reply_failed(reply, sent_ms, self._agent_failed(exc))
        if scope.cancelled_caught:
            return self._reply_failed(reply, sent_ms, self._deadline_missed(remaining_s))
        self._log_reply(reply, sent_ms)
        if reply.handoff:
            return _Outcome("ok", EndReason.HANDOFF)
        if reply.ended:
            return _Outcome("ok", EndReason.AGENT_ENDED)
        return None

    async def _stream_reply(self, stream: AsyncIterator[AgentChunk], reply: _Reply) -> None:
        """Read the reply up to its ``final`` chunk, then finalize the stream.

        Finalizing is shielded and bounded: when the per-turn deadline cancels the read, the
        adapter must still release its connection, but a stuck adapter cannot hang the trial.
        """
        try:
            async for chunk in stream:
                self._record(chunk, reply)
                if chunk.final:
                    break
        finally:
            if isinstance(stream, AsyncGenerator):
                with anyio.move_on_after(self._ctx.timeout_s, shield=True):
                    await stream.aclose()

    def _record(self, chunk: AgentChunk, reply: _Reply) -> None:
        """Log tool and handoff events as they stream; collect text with its timing."""
        now = self._sink.now_ms()
        if chunk.text:
            reply.parts.append(chunk.text)
            reply.first_ms = now if reply.first_ms is None else reply.first_ms
            reply.last_ms = now
        if chunk.tool_call is not None:
            self._sink.emit("agent", chunk.tool_call)
        if chunk.tool_result is not None:
            self._sink.emit("tool", chunk.tool_result)
        if chunk.handoff is not None:
            self._sink.emit("agent", chunk.handoff)
            reply.handoff = True
        reply.ended = reply.ended or chunk.ended

    def _log_reply(self, reply: _Reply, sent_ms: int) -> None:
        if reply.first_ms is None or reply.last_ms is None:
            now = self._sink.now_ms()
            self._log_turn("agent", "", t_ms=now, end_ms=now, latency_ms=None)
            return
        self._log_turn(
            "agent",
            reply.text,
            t_ms=reply.first_ms,
            end_ms=reply.last_ms,
            latency_ms=reply.first_ms - sent_ms,
        )

    def _log_turn(
        self, party: Party, text: str, *, t_ms: int, end_ms: int, latency_ms: int | None
    ) -> None:
        payload = UtterancePayload(text=text, turn=self.turns, end_ms=end_ms, latency_ms=latency_ms)
        self._sink.emit(party, payload, t_ms=t_ms)
        self._dialogue.append(DialogueTurn(speaker=party, text=text))

    # --- failures ----------------------------------------------------------------------------

    def _reply_failed(self, reply: _Reply, sent_ms: int, outcome: _Outcome) -> _Outcome:
        """Keep whatever the agent said before failing, so the transcript shows it."""
        if reply.parts:
            self._log_reply(reply, sent_ms)
        return outcome

    def _deadline_missed(self, remaining_s: float) -> _Outcome:
        if remaining_s < self._ctx.timeout_s:
            message = f"scenario max_duration_s={self._limits.max_duration_s} ran out mid-reply"
            return self._failed("timeout", message, EndReason.MAX_DURATION)
        return self._failed(
            "timeout", f"agent did not finish its reply within {self._ctx.timeout_s} s"
        )

    def _agent_failed(self, exc: AgentError) -> _Outcome:
        if isinstance(exc, AgentTimeoutError):
            return self._failed("timeout", str(exc))
        return self._failed("agent_error", str(exc))

    def _failed(
        self, status: DriverStatus, message: str, end_reason: EndReason = EndReason.ERROR
    ) -> _Outcome:
        self._log_error(message)
        return _Outcome(status, end_reason, message)

    def _log_error(self, message: str) -> None:
        self._sink.emit("system", ErrorPayload(source="agent", message=message))


async def _close_after_failure(session: ChatSession, timeout_s: float, exc: BaseException) -> None:
    """Close the session while a harness failure or cancellation propagates.

    Shielded, so a cancelled trial still releases the agent's session; a close failure is noted
    on the original exception rather than replacing it.
    """
    with anyio.move_on_after(timeout_s, shield=True):
        try:
            await session.close()
        except AgentError as close_exc:
            exc.add_note(f"closing the agent session also failed: {close_exc}")
