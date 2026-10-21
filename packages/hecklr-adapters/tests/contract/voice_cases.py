"""The voice contract's cases: each voice adapter configuration with its reference agent.

A case knows what its transport can promise (lossless audio, an abnormal close, how a
barge-in is stopped) so the contract asks the same questions of every adapter and holds each
to the strongest answer its transport allows.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import anyio

from hecklr_adapters.voice import WebSocketVoiceAdapterConfig, WebSocketVoiceTarget
from hecklr_schemas import VoiceTarget
from hecklr_voice.testing import synthetic_reply

from .voice_reference import (
    FRAME_MS,
    CallLog,
    LoopbackBrain,
    Protocol,
    Rhythm,
    WebSocketAgent,
    poll_until,
    serve_websocket,
)

CASE_DEADLINE_S = 60


@dataclass
class ReferenceCall:
    """A running reference agent and the adapter target pointed at it."""

    target: VoiceTarget
    calls: list[CallLog]

    async def call(self, index: int) -> CallLog:
        """The ``index``-th call the agent took, once it has arrived."""
        with anyio.fail_after(5):
            await poll_until(lambda: len(self.calls) > index)
        return self.calls[index]

    async def wait_hung_up(self, call: CallLog) -> None:
        """Until the agent has seen the caller leave."""
        with anyio.fail_after(5):
            await poll_until(lambda: call.caller_hung_up)


@dataclass
class VoiceCase:
    """One voice adapter configuration under contract test."""

    name: str
    wire_rate: int
    lossless: bool
    """Audio crosses unchanged, so the tagged reply transcribes exactly."""
    clears: bool
    """The agent stops by telling the transport to drop queued audio, not by going quiet."""
    can_fail: bool = True
    transport_delay_ms: float = 0.0
    """Codec and jitter-buffer delay the transport adds to every agent timing."""
    protocol: Protocol = "pcm"
    rhythm: Rhythm = "paced"
    extra: dict[str, Any] = field(default_factory=dict)

    def require(self) -> None:
        """Skip when this machine cannot run the case's reference agent."""

    def reaction_window(self, reaction_ms: float) -> tuple[float, float]:
        """Where the measured barge-in reaction must fall, before the tolerance.

        A ``clear`` drops audio that is still queued, but the 20 ms frame already in the
        caller's earpiece plays out, as on a phone: the heard stop is up to one frame later.
        """
        late = FRAME_MS if self.clears else 0.0
        return reaction_ms + self.transport_delay_ms, reaction_ms + self.transport_delay_ms + late

    @asynccontextmanager
    async def agent(
        self,
        *,
        reply: Any = None,
        reply_after_ms: float = 600.0,
        reaction_ms: float | None = 200.0,
        greeting: bool = False,
        hang_up_after: int | None = None,
        fail_after_replies: int | None = None,
    ) -> AsyncIterator[ReferenceCall]:
        """Start this case's reference agent and point the adapter at it."""

        def make_brain() -> LoopbackBrain:
            return LoopbackBrain(
                reply=synthetic_reply() if reply is None else reply,
                reply_after_ms=reply_after_ms,
                reaction_ms=reaction_ms,
                greeting=greeting,
                hang_up_after=hang_up_after,
            )

        # A contract failure must fail the test, not hang the suite.
        with anyio.fail_after(CASE_DEADLINE_S):
            async with self._serve(make_brain, fail_after_replies) as reference:
                yield reference

    @asynccontextmanager
    async def _serve(
        self, make_brain: Callable[[], LoopbackBrain], fail_after_replies: int | None
    ) -> AsyncIterator[ReferenceCall]:
        agent = WebSocketAgent(
            self.protocol, self.wire_rate, self.rhythm, make_brain, fail_after_replies
        )
        with serve_websocket(agent) as url:
            config = WebSocketVoiceAdapterConfig.model_validate(
                {"type": "websocket", "url": url, "protocol": self.protocol, **self.extra}
            )
            yield ReferenceCall(WebSocketVoiceTarget(config), agent.calls)


VOICE_CASES = [
    VoiceCase("websocket-pcm", 16_000, lossless=True, clears=False),
    VoiceCase(
        "websocket-pcm-24k-burst",
        24_000,
        lossless=False,
        clears=True,
        rhythm="burst",
        extra={"sample_rate": 24_000},
    ),
    VoiceCase(
        "websocket-twilio", 8_000, lossless=False, clears=True, protocol="twilio", rhythm="burst"
    ),
    VoiceCase("websocket-pipecat", 16_000, lossless=True, clears=False, protocol="pipecat"),
]
