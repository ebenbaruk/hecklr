"""The ``pipecat`` voice adapter: calls a Pipecat SmallWebRTC endpoint (extra ``pipecat``).

The caller is a WebRTC peer built with aiortc, as Pipecat's own SmallWebRTC clients are: it
offers one ``sendrecv`` audio transceiver, POSTs the offer to the endpoint (Pipecat's runner
serves it at ``/api/offer``) as ``{"sdp", "type", "request_data"}`` and applies the answer. The
trial id travels in the ``X-Hecklr-Trial-Id`` header and in ``request_data``. The call is
answered when the agent's audio track arrives; it ends when that track ends.

Pipecat agents reached over WebSocket instead use the ``websocket`` adapter with
``protocol: pipecat``.
"""

import asyncio
import contextlib
import fractions
import json
from collections.abc import AsyncIterator
from typing import Final

import anyio
import av
import httpx2
import numpy as np
import numpy.typing as npt
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError

from hecklr_adapters._http import agent_http_errors, check_status, parse_json
from hecklr_adapters.errors import AgentError
from hecklr_adapters.voice.audio import SAMPLE_RATE, ToHarness
from hecklr_adapters.voice.playout import Playout, Receiver
from hecklr_adapters.voice.websocket import check_caller_rate
from hecklr_schemas import PipecatAdapterConfig, SessionContext, TimedFrame

_TIME_BASE: Final = fractions.Fraction(1, SAMPLE_RATE)
_LEFT_POLL_S: Final = 0.05
"""How often a quiet receive checks whether the agent has left."""


def _mono(frame: av.AudioFrame) -> npt.NDArray[np.int16]:
    """The frame's samples, channels averaged."""
    data = frame.to_ndarray()
    # Planar audio is one row per channel; packed audio interleaves them in one row.
    if frame.format.is_planar:
        mixed = data.mean(axis=0)
    else:
        mixed = data.reshape(-1, len(frame.layout.channels)).mean(axis=1)
    return mixed.astype(np.int16)


class CallerTrack(MediaStreamTrack):
    """The caller's microphone: hands aiortc each frame the driver sends, when it sends it."""

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._frames: asyncio.Queue[av.AudioFrame] = asyncio.Queue()
        self._pts = 0

    def push(self, pcm: bytes) -> None:
        """Queue one frame of 16 kHz mono PCM for sending."""
        samples = len(pcm) // 2
        frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
        frame.planes[0].update(pcm)
        frame.sample_rate = SAMPLE_RATE
        frame.time_base = _TIME_BASE
        frame.pts = self._pts
        self._pts += samples
        self._frames.put_nowait(frame)

    async def recv(self) -> av.AudioFrame:
        """The next caller frame; aiortc's sender is paced by the driver's frames."""
        if self.readyState != "live":
            raise MediaStreamError
        return await self._frames.get()


class PipecatVoiceTarget:
    """Places each call as a new SmallWebRTC peer connection."""

    def __init__(self, config: PipecatAdapterConfig, timeout_s: float = 10.0) -> None:
        self._url = str(config.url)
        self._timeout_s = timeout_s

    async def connect(self, ctx: SessionContext) -> "PipecatAudioLink":
        """Offer, answer, and wait for the agent's audio track."""
        link = PipecatAudioLink()
        try:
            answer = await self._negotiate(ctx, await link.offer())
            await link.answer(answer)
        except BaseException:
            with anyio.CancelScope(shield=True):
                await link.hangup()
            raise
        return link

    async def _negotiate(
        self, ctx: SessionContext, offer: RTCSessionDescription
    ) -> RTCSessionDescription:
        body = {"sdp": offer.sdp, "type": offer.type, "request_data": {"trial_id": ctx.trial_id}}
        with agent_http_errors(self._url, self._timeout_s):
            async with httpx2.AsyncClient(timeout=httpx2.Timeout(self._timeout_s)) as client:
                response = await client.post(self._url, json=body, headers=ctx.headers())
                await check_status(response)
        data = parse_json(response.text)
        if not isinstance(data, dict) or not isinstance(sdp := data.get("sdp"), str):
            raise AgentError("agent's SmallWebRTC answer has no sdp")
        return RTCSessionDescription(sdp=sdp, type=str(data.get("type", "answer")))


class PipecatAudioLink:
    """One call over a WebRTC peer connection."""

    def __init__(self) -> None:
        self._pc = RTCPeerConnection()
        self._caller = CallerTrack()
        self._pc.addTransceiver(self._caller, direction="sendrecv")
        # Pipecat announces a hang-up on the data channel ("peerLeft") before closing, which
        # is how its clients learn of it promptly.
        self._pc.createDataChannel("hecklr").on("message", self._on_message)
        self._agent: MediaStreamTrack | None = None
        self._settled = anyio.Event()
        """Set once the connection is up or has failed."""
        self._to_harness = ToHarness()
        self._receiver = Receiver(Playout(), self._receive)
        self._hung_up = False
        self._agent_left = False
        self._pc.on("track", self._on_track)
        self._pc.on("connectionstatechange", self._on_state)

    async def offer(self) -> RTCSessionDescription:
        """The local offer, with every ICE candidate gathered (aiortc does not trickle)."""
        await self._pc.setLocalDescription(await self._pc.createOffer())
        description: RTCSessionDescription = self._pc.localDescription
        return description

    async def answer(self, answer: RTCSessionDescription) -> None:
        """Apply the agent's answer and wait until media flows (the connection is up)."""
        try:
            await self._pc.setRemoteDescription(answer)
        except ValueError as exc:
            raise AgentError(f"agent's SmallWebRTC answer is invalid: {exc}") from exc
        await self._settled.wait()
        if self._agent is None or self._pc.connectionState != "connected":
            raise AgentError("the WebRTC connection failed before the agent answered")
        self._receiver.start()

    async def send_frame(self, pcm: bytes, sample_rate: int) -> None:
        """Hand one caller frame to the WebRTC sender."""
        check_caller_rate(sample_rate)
        if self._hung_up:
            raise AgentError("the call is over: it was hung up")
        self._caller.push(pcm)

    def frames(self) -> AsyncIterator[TimedFrame]:
        """The agent's audio as heard in real time, until its track ends."""
        return self._receiver.frames()

    async def hangup(self) -> None:
        """Close the peer connection; safe to call twice."""
        if self._hung_up:
            return
        self._hung_up = True
        await self._receiver.stop()
        self._caller.stop()
        await self._pc.close()

    async def _receive(self) -> bool:
        track = self._agent
        if track is None:
            raise RuntimeError("receiving before the call was answered")
        while not self._agent_left:
            with anyio.move_on_after(_LEFT_POLL_S):
                try:
                    frame = await track.recv()
                except MediaStreamError:
                    return False
                if isinstance(frame, av.AudioFrame):
                    self._receiver.playout.write(self._to_harness(_mono(frame), frame.sample_rate))
                return True
        return False

    def _on_track(self, track: MediaStreamTrack) -> None:
        if track.kind == "audio" and self._agent is None:
            self._agent = track

    def _on_message(self, message: str | bytes) -> None:
        with contextlib.suppress(ValueError):  # app messages that are not JSON carry nothing
            data = json.loads(message)
            if isinstance(data, dict) and data.get("type") == "signalling":
                signal = data.get("message")
                if isinstance(signal, dict) and signal.get("type") == "peerLeft":
                    self._agent_left = True

    def _on_state(self) -> None:
        if self._pc.connectionState in ("connected", "failed", "closed"):
            self._settled.set()
