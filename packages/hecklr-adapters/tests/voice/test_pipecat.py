"""The ``pipecat`` adapter's own pieces: the caller track over real WebRTC, and negotiation."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import av
import numpy as np
import pytest
from aiortc import MediaStreamTrack, RTCPeerConnection
from aiortc.mediastreams import MediaStreamError

from hecklr_adapters import AgentError
from hecklr_adapters.voice.pipecat import CallerTrack, PipecatVoiceTarget
from hecklr_schemas import PipecatAdapterConfig, SessionContext, TrialId, new_id
from hecklr_voice.audio import speech_edges
from hecklr_voice.testing import synthetic_reply

pytestmark = [pytest.mark.anyio, pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]


def ctx() -> SessionContext:
    return SessionContext(trial_id=TrialId(new_id()), seed=1)


async def test_caller_audio_crosses_webrtc_with_its_speech_edges_in_place() -> None:
    """The caller track loses nothing and moves nothing: edges within the Opus codec delay."""
    clip = np.concatenate(
        [np.zeros(8000, np.int16), synthetic_reply("sure I can help"), np.zeros(8000, np.int16)]
    )
    sender, receiver = RTCPeerConnection(), RTCPeerConnection()
    track = CallerTrack()
    sender.addTransceiver(track, direction="sendonly")
    arrived: asyncio.Future[MediaStreamTrack] = asyncio.get_running_loop().create_future()
    receiver.on("track", arrived.set_result)
    await sender.setLocalDescription(await sender.createOffer())
    await receiver.setRemoteDescription(sender.localDescription)
    await receiver.setLocalDescription(await receiver.createAnswer())
    await sender.setRemoteDescription(receiver.localDescription)
    remote = await arrived
    heard: list[np.ndarray] = []

    async def listen() -> None:
        while True:
            frame = await remote.recv()
            assert isinstance(frame, av.AudioFrame)
            heard.append(frame.to_ndarray().reshape(-1, 2).mean(axis=1).astype(np.int16))

    listening = asyncio.get_running_loop().create_task(listen())
    start = time.monotonic()
    for index in range(len(clip) // 320):
        await asyncio.sleep(max(0.0, start + index * 0.02 - time.monotonic()))
        track.push(clip[index * 320 : (index + 1) * 320].tobytes())
    await asyncio.sleep(0.3)
    listening.cancel()
    await sender.close()
    await receiver.close()
    sent = speech_edges(clip)
    got = speech_edges(np.concatenate(heard))
    assert sent is not None
    assert got is not None
    sent_ms = (sent[1] - sent[0]) / 16
    got_ms = (got[1] - got[0]) / 48
    assert abs(got_ms - sent_ms) <= 10


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
