"""The ``pipecat`` adapter's own pieces: the caller track over real WebRTC, and negotiation."""

import asyncio
import json
import socket
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import anyio
import av
import numpy as np
import pytest
from aioice import stun
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceServer,
    RTCOutboundRtpStreamStats,
    RTCPeerConnection,
)
from aiortc.mediastreams import MediaStreamError

from hecklr_adapters import AgentError
from hecklr_adapters.voice.pipecat import (
    CallerTrack,
    PipecatAudioLink,
    PipecatVoiceTarget,
    peer_configuration,
)
from hecklr_schemas import PipecatAdapterConfig, SessionContext, TrialId, new_id
from hecklr_voice.audio import speech_edges
from hecklr_voice.testing import synthetic_reply

pytestmark = [pytest.mark.anyio, pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]


LOCAL = RTCConfiguration(iceServers=[])
"""Peers on this machine need no STUN server (and unit tests never touch the network)."""


def ctx() -> SessionContext:
    return SessionContext(trial_id=TrialId(new_id()), seed=1)


@asynccontextmanager
async def heard_by_aiortc(
    hear: Callable[[av.AudioFrame], None],
) -> AsyncIterator[tuple[CallerTrack, RTCPeerConnection]]:
    """A caller track sent to a plain aiortc peer that hands each frame it decodes to ``hear``."""
    sender, receiver = RTCPeerConnection(LOCAL), RTCPeerConnection(LOCAL)
    track = CallerTrack()
    sender.addTransceiver(track, direction="sendonly")
    arrived: asyncio.Future[MediaStreamTrack] = asyncio.get_running_loop().create_future()
    receiver.on("track", arrived.set_result)
    await sender.setLocalDescription(await sender.createOffer())
    await receiver.setRemoteDescription(sender.localDescription)
    await receiver.setLocalDescription(await receiver.createAnswer())
    await sender.setRemoteDescription(receiver.localDescription)
    remote = await arrived

    async def listen() -> None:
        while True:
            frame = await remote.recv()
            assert isinstance(frame, av.AudioFrame)
            hear(frame)

    listening = asyncio.get_running_loop().create_task(listen())
    try:
        yield track, sender
    finally:
        listening.cancel()
        await sender.close()
        await receiver.close()


async def play(track: CallerTrack, clip: np.ndarray) -> None:
    """Push ``clip`` in real-time 20 ms frames."""
    start = time.monotonic()
    for index in range(len(clip) // 320):
        await asyncio.sleep(max(0.0, start + index * 0.02 - time.monotonic()))
        track.push(clip[index * 320 : (index + 1) * 320].tobytes())


async def test_caller_audio_crosses_webrtc_with_its_speech_edges_in_place() -> None:
    """The caller track loses nothing and moves nothing: edges within the Opus codec delay."""
    clip = np.concatenate(
        [np.zeros(8000, np.int16), synthetic_reply("sure I can help"), np.zeros(8000, np.int16)]
    )
    heard: list[np.ndarray] = []

    def hear(frame: av.AudioFrame) -> None:
        heard.append(frame.to_ndarray().reshape(-1, 2).mean(axis=1).astype(np.int16))

    async with heard_by_aiortc(hear) as (track, _):
        await play(track, clip)
        await asyncio.sleep(0.3)
    sent = speech_edges(clip)
    got = speech_edges(np.concatenate(heard))
    assert sent is not None
    assert got is not None
    sent_ms = (sent[1] - sent[0]) / 16
    got_ms = (got[1] - got[0]) / 48
    assert abs(got_ms - sent_ms) <= 10


async def test_issue27_aiortc_holds_the_last_five_frames_of_a_stream_until_more_follow() -> None:
    """Why the voice contract keeps the caller's line open after speaking.

    The sender's encoder holds the last frame (it resamples to 48 kHz in 20 ms blocks) and the
    receiver's audio jitter buffer (capacity 16, prefetch 4) releases a frame only once four
    more are behind it. So a stream that stops leaves its last 100 ms undelivered, and a stream
    that goes on, as the driver's line does all call long, only delays them. This is the
    ~110 ms of each caller utterance the Pipecat reference bot "lost" (issue #27).
    """
    received = 0

    def hear(frame: av.AudioFrame) -> None:
        nonlocal received
        received += 1

    async with heard_by_aiortc(hear) as (track, sender):
        await play(track, np.zeros(50 * 320, np.int16))
        await asyncio.sleep(0.5)
        stats = (await sender.getStats()).values()
        [outbound] = [s for s in stats if isinstance(s, RTCOutboundRtpStreamStats)]
        held_by_sender, held_by_receiver = (
            50 - outbound.packetsSent,
            outbound.packetsSent - received,
        )
        await play(track, np.zeros(5 * 320, np.int16))  # the line goes on
        await asyncio.sleep(0.3)
    assert (held_by_sender, held_by_receiver) == (1, 4)
    assert received >= 50


async def test_caller_track_refuses_reads_once_stopped() -> None:
    track = CallerTrack()
    track.stop()
    with pytest.raises(MediaStreamError):
        await track.recv()


Handler = Callable[[bytes], tuple[int, bytes]]


@asynccontextmanager
async def http_agent(handler: Handler) -> AsyncIterator[str]:
    """A bare HTTP endpoint answering every request with ``handler(body)``."""

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        length = next(
            int(line.split(b":")[1])
            for line in head.split(b"\r\n")
            if line.lower().startswith(b"content-length")
        )
        status, body = handler(await reader.readexactly(length))
        writer.write(
            f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/offer"
    finally:
        server.close()


def target(url: str) -> PipecatVoiceTarget:
    return PipecatVoiceTarget(PipecatAdapterConfig.model_validate({"type": "pipecat", "url": url}))


async def test_offer_carries_the_trial_and_an_http_error_is_an_agent_error() -> None:
    offers: list[dict[str, object]] = []

    def fail(body: bytes) -> tuple[int, bytes]:
        offers.append(json.loads(body))
        return 500, b'{"error": "no bot"}'

    context = ctx()
    async with http_agent(fail) as url:
        with pytest.raises(AgentError, match="HTTP 500"):
            await target(url).connect(context)
    [offer] = offers
    assert offer["type"] == "offer"
    assert isinstance(offer["sdp"], str)
    assert "m=audio" in offer["sdp"]
    assert offer["request_data"] == {"trial_id": context.trial_id}


@pytest.mark.parametrize(
    ("answer", "error"),
    [(b'{"type": "answer"}', "no sdp"), (b'{"sdp": "v=0", "type": "answer"}', "invalid")],
)
async def test_unusable_answers_are_agent_errors(answer: bytes, error: str) -> None:
    async with http_agent(lambda _: (200, answer)) as url:
        with pytest.raises(AgentError, match=error):
            await target(url).connect(ctx())


async def test_unreachable_endpoint_is_an_agent_error() -> None:
    async with http_agent(lambda _: (200, b"{}")) as url:
        pass
    with pytest.raises(AgentError):
        await target(url).connect(ctx())


async def test_hangup_closes_the_peer_connection_even_when_a_step_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    link = PipecatAudioLink()

    async def broken_stop() -> None:
        raise RuntimeError("reader would not stop")

    monkeypatch.setattr(link._receiver, "stop", broken_stop)
    with pytest.raises(RuntimeError, match="would not stop"):
        await link.hangup()
    assert link._pc.connectionState == "closed"
    assert link._caller.readyState == "ended"


async def answered_link() -> tuple[PipecatAudioLink, RTCPeerConnection]:
    """A link whose call a plain aiortc peer has answered, with ICE completed."""
    link, agent = (
        PipecatAudioLink(peer_configuration("http://127.0.0.1/api/offer")),
        RTCPeerConnection(LOCAL),
    )
    agent.addTrack(CallerTrack())
    await agent.setRemoteDescription(await link.offer())
    await agent.setLocalDescription(await agent.createAnswer())
    with anyio.fail_after(10):
        await link.answer(agent.localDescription)
    return link, agent


async def test_issue73_hangup_leaves_no_ice_check_retrying_on_a_closed_socket() -> None:
    """A connectivity check the agent triggers after ICE completed dies with the call.

    The agent's checks can reach the caller after ICE has completed (from another interface,
    such as a link-local one), and each starts a triggered check back that retries every
    0.5 s, 1 s, ... until answered. aioice does not stop it on close, so its retry fired on the
    closed socket and raised inside the event loop, failing whatever test was running.
    """
    link, agent = await answered_link()
    silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # a peer that never answers
    try:
        [ice] = {
            transceiver.receiver.transport.transport for transceiver in link._pc.getTransceivers()
        }
        connection = ice.iceGatherer._connection
        local = next(  # an IPv4 host candidate the silent peer can sit beside
            pair for pair in connection._check_list if "." in pair.local_candidate.host
        )
        silent.bind((local.local_candidate.host, 0))
        check = stun.Message(stun.Method.BINDING, stun.Class.REQUEST)
        check.attributes["PRIORITY"] = 1
        connection.check_incoming(check, silent.getsockname(), local.protocol)
        assert any(pair.task is not None for pair in connection._check_list)
        await link.hangup()
        await anyio.sleep(stun.RETRY_RTO * 1.5)  # past the first retry
    finally:
        silent.close()
        await agent.close()
    assert all(pair.task is None or pair.task.done() for pair in connection._check_list)


@pytest.mark.parametrize(
    ("url", "stun"),
    [
        ("http://127.0.0.1:7860/api/offer", False),
        ("http://localhost:7860/api/offer", False),
        ("http://[::1]:7860/api/offer", False),
        ("https://bot.example.com/api/offer", True),
        ("http://10.0.0.7:7860/api/offer", True),
    ],
)
def test_issue73_only_an_agent_off_this_machine_is_called_with_a_stun_server(
    url: str, stun: bool
) -> None:
    configuration = peer_configuration(url)
    # None is aiortc's default STUN server; an empty list is none at all.
    assert (configuration.iceServers is None) is stun
    assert configuration.bundlePolicy.value == "max-bundle"


async def test_issue73_hangup_during_gathering_leaves_no_stun_query_behind() -> None:
    """A call given up while its STUN query is unanswered closes only once gathering ended.

    Cancelling gathering left aioice's STUN query retrying on a socket that hang-up closed.
    """
    silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # a STUN server that never answers
    silent.bind(("127.0.0.1", 0))
    stun_url = f"stun:127.0.0.1:{silent.getsockname()[1]}"
    link = PipecatAudioLink(RTCConfiguration(iceServers=[RTCIceServer(stun_url)]))
    try:
        with anyio.move_on_after(0.2):
            await link.offer()
        await link.hangup()
        await anyio.sleep(stun.RETRY_RTO * 1.5)  # past the next retry
    finally:
        silent.close()
    assert link._pc.connectionState == "closed"
