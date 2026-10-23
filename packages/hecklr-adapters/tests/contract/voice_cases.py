"""The voice contract's cases: each voice adapter configuration with its reference agent.

A case knows what its transport can promise (lossless audio, an abnormal close, how a
barge-in is stopped, how much delay its codecs add) so the contract asks the same questions of
every adapter and holds each to the strongest answer its transport allows.
"""

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal

import anyio
import pytest

from hecklr_adapters.voice import WebSocketVoiceAdapterConfig, WebSocketVoiceTarget
from hecklr_schemas import VoiceTarget
from hecklr_voice.testing import synthetic_reply

from .voice_reference import (
    CallLog,
    LoopbackBrain,
    Protocol,
    Rhythm,
    WebSocketAgent,
    poll_until,
    serve_websocket,
)

CASE_DEADLINE_S = 60
TOLERANCE_MS = 20
"""SC4: measured voice timings within 20 ms of the truth."""
Transport = Literal["websocket", "livekit", "pipecat"]


@dataclass
class ReferenceCall:
    """A running reference agent and the adapter target pointed at it."""

    target: VoiceTarget
    calls: list[CallLog]

    async def call(self, index: int) -> CallLog:
        """The ``index``-th call the agent took, once it has arrived."""
        with anyio.fail_after(10):
            await poll_until(lambda: len(self.calls) > index)
        return self.calls[index]

    async def wait_hung_up(self, call: CallLog) -> None:
        """Until the agent has seen the caller leave."""
        with anyio.fail_after(10):
            await poll_until(lambda: call.caller_hung_up)


@dataclass
class VoiceCase:
    """One voice adapter configuration under contract test."""

    name: str
    wire_rate: int
    """The rate caller audio reaches the agent at."""
    lossless: bool
    """Audio crosses unchanged, so the tagged reply transcribes exactly."""
    clears: bool
    """The agent stops by telling the transport to drop queued audio, not by going quiet."""
    transport: Transport = "websocket"
    can_fail: bool = True
    """The transport can end a call abnormally (as opposed to the agent just leaving)."""
    offset_ms: tuple[float, float] = (0.0, 0.0)
    """Range of the round-trip delay the transport itself adds (codecs, jitter buffers).

    It is real latency a caller hears, so Hecklr measures it on top of the agent's own delay;
    the contract checks that it is bounded and the same for every reply.
    """
    jitter_ms: float = 0.0
    """How much the transport's own delay may wander during a call.

    WebRTC receivers adapt their jitter buffers as a call goes on, so the delay a caller hears
    moves by tens of milliseconds; a sample-preserving WebSocket keeps it fixed.
    """
    timing_needs_idle_cpu: bool = False
    """This case's real-time checks (SC4 included) are only meaningful on an otherwise idle CPU.

    WebRTC jitter buffers grow when the CPU is busy, and the reference bots share this process
    (and its GIL) with the harness: under a parallel test run their delay climbs by hundreds
    of milliseconds, which measures the test machine, not the adapter. (The SC4 tests
    themselves are ``sc4_timing``: they run serially, and for these cases only on a dedicated
    CPU, not on CI's shared runner: ADR 0010b.)
    """
    protocol: Protocol = "pcm"
    rhythm: Rhythm = "paced"
    extra: dict[str, Any] = field(default_factory=dict)
    environment: Any = None
    """What the reference agent needs from the test session (a LiveKit server)."""

    @property
    def timing_is_meaningful(self) -> bool:
        """Whether this run can judge the case's timing: not a load-sensitive case under xdist or
        on a shared CPU (``HECKLR_SHARED_CPU``, set by CI; ADR 0010b)."""
        busy = os.environ.get("PYTEST_XDIST_WORKER") or os.environ.get("HECKLR_SHARED_CPU") == "1"
        return not (self.timing_needs_idle_cpu and busy)

    def require_idle_cpu_for_timing(self) -> None:
        """Skip an SC4 test for this case where its timing is not meaningful."""
        if not self.timing_is_meaningful:
            pytest.skip(f"{self.name}: SC4 over WebRTC needs a dedicated idle CPU (ADR 0010b)")

    def check_offsets(self, offsets: list[float], spread_ms: float) -> None:
        """SC4 over this transport: every timing error lies in the transport's delay window
        within the 20 ms tolerance, and all errors agree within 2 x 20 ms plus ``spread_ms``.

        ``spread_ms`` covers a ``clear``: queued audio is dropped, but the 20 ms frame already
        in the caller's earpiece plays out, as on a phone.
        """
        low, high = self.offset_ms
        assert offsets
        assert all(low - TOLERANCE_MS <= o <= high + spread_ms + TOLERANCE_MS for o in offsets), (
            self.name,
            offsets,
        )
        spread = max(offsets) - min(offsets)
        assert spread <= 2 * TOLERANCE_MS + spread_ms + self.jitter_ms, (self.name, offsets)

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
            match self.transport:
                case "websocket":
                    serving = self._websocket(make_brain, fail_after_replies)
                case "livekit":
                    serving = self._livekit(make_brain)
                case "pipecat":
                    serving = self._pipecat(make_brain)
            async with serving as reference:
                yield reference

    @asynccontextmanager
    async def _websocket(
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

    @asynccontextmanager
    async def _livekit(
        self, make_brain: Callable[[], LoopbackBrain]
    ) -> AsyncIterator[ReferenceCall]:
        from hecklr_adapters.voice.livekit import LiveKitVoiceTarget
        from hecklr_schemas import LiveKitAdapterConfig, new_id

        from .voice_livekit import API_KEY, API_SECRET, LiveKitAgent, run_livekit_agent

        prefix = f"contract-{new_id().lower()}"
        agent = LiveKitAgent(self.environment, prefix, make_brain, self.rhythm)
        config = LiveKitAdapterConfig(type="livekit", url=self.environment.url, room_prefix=prefix)
        environ = {"LIVEKIT_API_KEY": API_KEY, "LIVEKIT_API_SECRET": API_SECRET}
        with run_livekit_agent(agent):
            yield ReferenceCall(LiveKitVoiceTarget(config, environ), agent.calls)

    @asynccontextmanager
    async def _pipecat(
        self, make_brain: Callable[[], LoopbackBrain]
    ) -> AsyncIterator[ReferenceCall]:
        from hecklr_adapters.voice.pipecat import PipecatVoiceTarget
        from hecklr_schemas import PipecatAdapterConfig

        from .voice_pipecat import PipecatAgent, serve_pipecat

        agent = PipecatAgent(make_brain)
        with serve_pipecat(agent) as url:
            config = PipecatAdapterConfig.model_validate({"type": "pipecat", "url": url})
            yield ReferenceCall(PipecatVoiceTarget(config), agent.calls)


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
    VoiceCase(
        "livekit",
        48_000,
        lossless=False,
        clears=True,
        transport="livekit",
        can_fail=False,
        offset_ms=(0.0, 350.0),  # 80-170 ms idle, up to ~340 ms on a loaded machine
        jitter_ms=60.0,
        rhythm="burst",
        timing_needs_idle_cpu=True,
    ),
    VoiceCase(
        "pipecat-smallwebrtc",
        48_000,
        lossless=False,
        clears=True,
        transport="pipecat",
        can_fail=False,
        offset_ms=(0.0, 400.0),
        # The reference bot's sender (Pipecat's RawAudioTrack) runs on a thread of this
        # process: when it is starved of CPU it falls behind and catches up in a burst, which
        # the caller hears as later audio for the rest of the call (ADR 00hfa, issue #27).
        jitter_ms=200.0,
        timing_needs_idle_cpu=True,
    ),
]
