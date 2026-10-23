"""The ``pipecat`` voice adapter: calls a Pipecat SmallWebRTC endpoint (extra ``pipecat``).

The caller is a WebRTC peer built with aiortc, as Pipecat's own SmallWebRTC clients are: it
offers one ``sendrecv`` audio transceiver, POSTs the offer to the endpoint (Pipecat's runner
serves it at ``/api/offer``) as ``{"sdp", "type", "request_data"}`` and applies the answer. The
trial id and its world URLs travel as headers (``session_headers``) and in ``request_data``
(``session_fields``). The call is answered when the agent's audio track arrives; it ends when
that track ends.

An agent on this machine is called without a STUN server: it reaches the caller's host
candidates directly, and querying a public server only delays the call (by 5 s when an
interface, such as a link-local one, has no route to it). Any other agent is called with
aiortc's default STUN server. Media and the data channel share one transport (max-bundle), as
on Pipecat's side.

Pipecat agents reached over WebSocket instead use the ``websocket`` adapter with
``protocol: pipecat``.
"""

import asyncio
import contextlib
import fractions
import ipaddress
import json
from collections.abc import AsyncIterator
from typing import Final
from urllib.parse import urlsplit

import anyio
import av
import httpx2
import numpy as np
import numpy.typing as npt
from aioice import Connection
from aiortc import (
    MediaStreamTrack,
    RTCBundlePolicy,
    RTCConfiguration,
    RTCIceTransport,
    RTCPeerConnection,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError

from hecklr_adapters._http import agent_http_errors, check_status, parse_json
from hecklr_adapters.context import session_fields, session_headers
from hecklr_adapters.errors import AgentError
from hecklr_adapters.voice.audio import SAMPLE_RATE, ToHarness
from hecklr_adapters.voice.playout import RELEASE_TIMEOUT_S, Playout, Receiver
from hecklr_adapters.voice.websocket import check_caller_rate
from hecklr_schemas import PipecatAdapterConfig, SessionContext, TimedFrame

_TIME_BASE: Final = fractions.Fraction(1, SAMPLE_RATE)
_LEFT_POLL_S: Final = 0.05
"""How often a quiet receive checks whether the agent has left."""
_GATHERING_LIMIT_S: Final = 6.0
"""Bound on waiting for ICE gathering at hang-up: aioice ends it by itself after 5 s."""


def peer_configuration(url: str) -> RTCConfiguration:
    """How to call the agent at ``url``: no STUN server when it runs on this machine."""
    host = urlsplit(url).hostname or ""
    try:
        local = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:  # a host name other than localhost
        local = False
    # aiortc reads iceServers=None as "its default STUN server".
    return RTCConfiguration(
        iceServers=[] if local else None, bundlePolicy=RTCBundlePolicy.MAX_BUNDLE
    )


def _mono(frame: av.AudioFrame) -> npt.NDArray[np.int16]:
    """The frame's samples, channels averaged."""
    data = frame.to_ndarray()
    # Planar audio is one row per channel; packed audio interleaves them in one row.
    if frame.format.is_planar:
        mixed = data.mean(axis=0)
    else:
        mixed = data.reshape(-1, len(frame.layout.channels)).mean(axis=1)
    return mixed.astype(np.int16)


async def _stop_checks(connections: list[Connection]) -> None:
    """Cancel every ICE connectivity check still running and wait until each has ended."""
    checks = [pair.task for c in connections for pair in c._check_list if pair.task is not None]
    for check in checks:
        check.cancel()
    if checks:
        # Waited for, not gathered: a check that failed rather than being cancelled must
        # still surface (asyncio reports a task exception nobody retrieved).
        await asyncio.wait(checks)


async def _close_peer(pc: RTCPeerConnection, ice_transports: list[RTCIceTransport]) -> None:
    """Close the peer connection and every ICE transport it gathered on, leaving nothing behind.

    A connectivity check the agent triggers after ICE completed (its checks arriving from
    another interface, such as a link-local one) starts a STUN transaction that retries at
    0.5 s, 1 s, 2 s, ... until answered. aioice 0.10's ``Connection.close`` does not cancel it,
    so its retry fired on the closed socket and raised inside the event loop (issue #73). The
    checks are reached through aioice's private state because nothing public exposes them.
    """
    connections = [ice.iceGatherer._connection for ice in ice_transports]
    await _stop_checks(connections)
    try:
        await pc.close()
    finally:
        for ice in ice_transports:
            await ice.stop()
        # The agent may have triggered another check while the transports were closing.
        await _stop_checks(connections)


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

    def __init__(self, config: PipecatAdapterConfig) -> None:
        self._url = str(config.url)

    async def connect(self, ctx: SessionContext) -> "PipecatAudioLink":
        """Offer, answer, and wait for the agent's audio track."""
        link = PipecatAudioLink(peer_configuration(self._url))
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
        body = {"sdp": offer.sdp, "type": offer.type, "request_data": session_fields(ctx)}
        # The schema's config has no timeout of its own; the trial's per-reply deadline is the
        # longest a caller would wait for the line to pick up.
        with agent_http_errors(self._url, ctx.timeout_s):
            async with httpx2.AsyncClient(timeout=httpx2.Timeout(ctx.timeout_s)) as client:
                response = await client.post(self._url, json=body, headers=session_headers(ctx))
                await check_status(response)
        data = parse_json(response.text)
        if not isinstance(data, dict) or not isinstance(sdp := data.get("sdp"), str):
            raise AgentError("agent's SmallWebRTC answer has no sdp")
        return RTCSessionDescription(sdp=sdp, type=str(data.get("type", "answer")))


class PipecatAudioLink:
    """One call over a WebRTC peer connection."""

    def __init__(self, configuration: RTCConfiguration | None = None) -> None:
        self._pc = RTCPeerConnection(configuration)
        self._caller = CallerTrack()
        self._pc.addTransceiver(self._caller, direction="sendrecv")
        # Pipecat announces a hang-up on the data channel ("peerLeft") before closing, which
        # is how its clients learn of it promptly.
        channel = self._pc.createDataChannel("hecklr")
        channel.on("message", self._on_message)
        # Every ICE transport (one under max-bundle), as hang-up must close each of them.
        self._ice = list(
            {t.receiver.transport.transport for t in self._pc.getTransceivers()}
            | {channel.transport.transport.transport}
        )
        self._gathering: asyncio.Task[None] | None = None
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
        # Gathering runs to its end even if the caller gives up: aioice queries the STUN server
        # in tasks that only it can cancel, when gathering ends, so a cancelled gathering would
        # leave queries retrying on the sockets hang-up closes. Hang-up waits for it instead.
        self._gathering = asyncio.ensure_future(
            self._pc.setLocalDescription(await self._pc.createOffer())
        )
        await asyncio.shield(self._gathering)
        description: RTCSessionDescription = self._pc.localDescription
        return description

    async def answer(self, answer: RTCSessionDescription) -> None:
        """Apply the agent's answer and wait until media flows (the connection is up)."""
        try:
            await self._pc.setRemoteDescription(answer)
        except ValueError as exc:
            raise AgentError(f"agent's SmallWebRTC answer is invalid: {exc}") from exc
        # No deadline here: ICE and DTLS have their own failure states, and the driver bounds
        # the whole connect by the trial's timeout (as LiveKit's start() relies on too).
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
        """Close the peer connection; safe to call twice.

        Every step runs even if an earlier one fails or the caller cancels the hang-up, so a
        failed call never leaves a peer connection or reading task behind.
        """
        if self._hung_up:
            return
        self._hung_up = True
        if self._gathering is not None:
            with anyio.move_on_after(_GATHERING_LIMIT_S, shield=True):
                await asyncio.wait([self._gathering])
        with anyio.move_on_after(RELEASE_TIMEOUT_S, shield=True):
            try:
                await self._receiver.stop()
            finally:
                self._caller.stop()
                await _close_peer(self._pc, self._ice)

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
