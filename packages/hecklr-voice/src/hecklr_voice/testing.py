"""An in-process loopback voice agent with known timing, for unit tests and SC4 calibration.

``LoopbackVoiceTarget`` answers every caller utterance with a fixed reply exactly
``reply_after_ms`` after the caller's last speech sample, and when the caller speaks over a reply
it stops exactly ``barge_in_reaction_ms`` after the caller's first speech sample. Hecklr must
measure both within 20 ms (SC4). Its stream is continuous and real-time, like WebRTC: silence
frames flow between replies, and every timing is placed at the sample, not the frame.

The networked loopback agent for the calibration CI job lives in ``examples/loopback-voice``;
this one needs no sockets.
"""

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final

import anyio
import numpy as np

from hecklr_adapters import AgentError
from hecklr_schemas import SessionContext, TimedFrame
from hecklr_voice.audio import (
    FRAME_MS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    SAMPLES_PER_MS,
    Pcm16,
    bytes_to_pcm16,
    frame_dbfs,
    speech_edges,
    to_pcm16,
)
from hecklr_voice.tts import SpeechRequest, synthetic_speech
from hecklr_voice.vad import DEFAULT_ENERGY_THRESHOLD_DBFS

DEFAULT_REPLY_TEXT: Final = (
    "thanks for calling acme maintenance I can move your boiler visit to any morning next week "
    "that suits you and send a confirmation by text message right away"
)
"""One phrase without punctuation: continuous speech, so a cut lands mid-sound."""

_NEVER: Final = 2**62
"""Stop position of a clip that plays to its end."""


def synthetic_reply(text: str = DEFAULT_REPLY_TEXT, voice: str = "loopback") -> Pcm16:
    """Tagged synthetic speech for a loopback reply (``FakeASR`` transcribes it exactly)."""
    return to_pcm16(synthetic_speech(SpeechRequest(text=text, voice=voice), tag=True))


@dataclass(slots=True)
class _Clip:
    start: int
    pcm: Pcm16
    stop: int

    @property
    def end(self) -> int:
        return min(self.start + len(self.pcm), self.stop)


class LoopbackAudioLink:
    """One loopback call. Inspect ``reply_starts_ms`` / ``reply_stops_ms`` in tests."""

    def __init__(
        self,
        reply: Pcm16,
        *,
        reply_after_ms: float,
        barge_in_reaction_ms: float | None,
        endpoint_ms: float,
        greeting: Pcm16 | None,
        hang_up_after_replies: int | None,
    ) -> None:
        self._reply = reply
        self._reply_after_ms = reply_after_ms
        self._reaction_ms = barge_in_reaction_ms
        self._endpoint_ms = endpoint_ms
        self._hang_up_after = hang_up_after_replies
        self._origin_ms = _now_ms()
        self._clips: list[_Clip] = []
        self._caller_speaking = False
        self._caller_last_ms = 0.0
        self._closed = False
        self.frames_received = 0
        self.reply_starts_ms: list[float] = []
        """Media time (ms since connect) of each reply's first sample."""
        self.reply_stops_ms: list[float] = []
        """Media time of each barge-in cut."""
        if greeting is not None:
            self._schedule(self._origin_ms + 200.0, greeting)

    async def send_frame(self, pcm: bytes, sample_rate: int) -> None:
        """Hear one caller frame: detect speech onsets and end of speech at the sample."""
        if self._closed:
            raise AgentError("the loopback call is over")
        if sample_rate != SAMPLE_RATE:
            raise AgentError(f"loopback expects {SAMPLE_RATE} Hz, got {sample_rate}")
        arrival = _now_ms()
        self.frames_received += 1
        samples = bytes_to_pcm16(pcm)
        edges = (
            speech_edges(samples) if frame_dbfs(samples) >= DEFAULT_ENERGY_THRESHOLD_DBFS else None
        )
        if edges is not None:
            if not self._caller_speaking:
                self._caller_speaking = True
                self._caller_started(arrival + edges[0] / SAMPLES_PER_MS)
            self._caller_last_ms = arrival + edges[1] / SAMPLES_PER_MS
        elif self._caller_speaking and arrival - self._caller_last_ms >= self._endpoint_ms:
            self._caller_speaking = False
            busy_until = max((self._time(clip.end) for clip in self._clips), default=0.0)
            self._schedule(
                max(self._caller_last_ms + self._reply_after_ms, busy_until), self._reply
            )

    async def frames(self) -> AsyncIterator[TimedFrame]:
        """A continuous real-time stream of 20 ms frames, silence between replies."""
        index = 0
        while not self._closed and not self._hung_up():
            due = self._origin_ms + index * FRAME_MS
            delay = due - _now_ms()
            if delay > 0:
                await anyio.sleep(delay / 1000)
            yield TimedFrame(
                pcm=self._render(index).astype("<i2").tobytes(),
                sample_rate=SAMPLE_RATE,
                sample_index=index * FRAME_SAMPLES,
                t_ms=max(0, round(_now_ms() - self._origin_ms)),
            )
            index += 1

    async def hangup(self) -> None:
        """End the call."""
        self._closed = True

    def _caller_started(self, onset_ms: float) -> None:
        if self._reaction_ms is None:
            return
        position = self._position(onset_ms)
        for clip in self._clips:
            if clip.start <= position < clip.end:
                clip.stop = self._position(onset_ms + self._reaction_ms)
                self.reply_stops_ms.append(self._time(clip.stop) - self._origin_ms)

    def _schedule(self, at_ms: float, pcm: Pcm16) -> None:
        clip = _Clip(self._position(at_ms), pcm, stop=_NEVER)
        self._clips.append(clip)
        self.reply_starts_ms.append(self._time(clip.start) - self._origin_ms)

    def _render(self, index: int) -> Pcm16:
        begin = index * FRAME_SAMPLES
        frame = np.zeros(FRAME_SAMPLES, dtype=np.int16)
        for clip in self._clips:
            low, high = max(begin, clip.start), min(begin + FRAME_SAMPLES, clip.end)
            if low < high:
                frame[low - begin : high - begin] = clip.pcm[low - clip.start : high - clip.start]
        return frame

    def _hung_up(self) -> bool:
        if self._hang_up_after is None or len(self._clips) < self._hang_up_after:
            return False
        return _now_ms() > max(self._time(clip.end) for clip in self._clips) + 500

    def _position(self, at_ms: float) -> int:
        return round((at_ms - self._origin_ms) * SAMPLES_PER_MS)

    def _time(self, position: int) -> float:
        return self._origin_ms + position / SAMPLES_PER_MS


class LoopbackVoiceTarget:
    """Connects :class:`LoopbackAudioLink` calls; every link is kept in ``links`` for inspection.

    Args:
        reply: What the agent says each time; tagged synthetic speech by default.
        reply_after_ms: Injected response delay, from the caller's last speech sample.
        barge_in_reaction_ms: Injected reaction to a barge-in; ``None`` never stops (like F8).
        endpoint_ms: Caller silence after which the agent decides the caller is done. It must
            be shorter than ``reply_after_ms``, which counts from the caller's last sound.
        greeting: Said 200 ms after connecting, if given.
        hang_up_after_replies: Hang up 500 ms after this many replies have played.
    """

    def __init__(
        self,
        *,
        reply: Pcm16 | None = None,
        reply_after_ms: float = 600.0,
        barge_in_reaction_ms: float | None = 200.0,
        endpoint_ms: float = 200.0,
        greeting: Pcm16 | None = None,
        hang_up_after_replies: int | None = None,
    ) -> None:
        if endpoint_ms + FRAME_MS > reply_after_ms:
            raise ValueError("reply_after_ms must exceed endpoint_ms by at least one frame")
        self._reply = synthetic_reply() if reply is None else reply
        self._options = (reply_after_ms, barge_in_reaction_ms, endpoint_ms, greeting)
        self._hang_up_after = hang_up_after_replies
        self.links: list[LoopbackAudioLink] = []

    async def connect(self, ctx: SessionContext) -> LoopbackAudioLink:
        """Answer the call."""
        reply_after_ms, reaction_ms, endpoint_ms, greeting = self._options
        link = LoopbackAudioLink(
            self._reply,
            reply_after_ms=reply_after_ms,
            barge_in_reaction_ms=reaction_ms,
            endpoint_ms=endpoint_ms,
            greeting=greeting,
            hang_up_after_replies=self._hang_up_after,
        )
        self.links.append(link)
        return link


def _now_ms() -> float:
    return time.monotonic_ns() / 1e6
