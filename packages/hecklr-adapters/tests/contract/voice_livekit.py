"""LiveKit side of the voice contract: a ``livekit-server`` in Docker and a loopback agent worker.

The server is the official image, started once per test process on free host ports (so
parallel workers each get their own) and skipped cleanly when Docker is unavailable. Its RTC
ports are advertised on 127.0.0.1 and published on the same host ports, as ICE requires.

The reference agent behaves like a LiveKit agent worker: it watches for new rooms with the
configured prefix, joins each one as ``agent``, listens to the caller's track and speaks the
loopback brain's replies through an ``AudioSource``, interrupting with ``clear_queue`` as
LiveKit agents do.
"""

import asyncio
import concurrent.futures
import contextlib
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Final

import anyio
import httpx2
import numpy as np
from livekit import api, rtc

from hecklr_voice.audio import Pcm16

from .voice_reference import AgentThread, CallLog, LoopbackBrain, Rhythm, Voice, now_ms, speak

IMAGE: Final = "livekit/livekit-server:v1.13.10"
API_KEY: Final = "hecklr-test"
API_SECRET: Final = "hecklr-test-secret-for-a-throwaway-local-server"
AGENT_RATE: Final = 48_000
"""The agent publishes and listens at WebRTC's native rate; LiveKit converts for the caller."""


@dataclass(frozen=True)
class LiveKitServer:
    """A running test server."""

    port: int

    @property
    def url(self) -> str:
        """Signal URL for rtc clients."""
        return f"ws://127.0.0.1:{self.port}"

    @property
    def http_url(self) -> str:
        """Server API URL."""
        return f"http://127.0.0.1:{self.port}"


def docker_unavailable() -> str | None:
    """Why LiveKit tests cannot run here, or ``None`` if they can."""
    docker = shutil.which("docker")
    if docker is None:
        return "docker is not installed"
    probe = subprocess.run([docker, "info"], capture_output=True, check=False, timeout=30)  # noqa: S603 -- fixed arguments
    if probe.returncode != 0:
        return "the docker daemon is not running"
    return None


def _free_port(kind: socket.SocketKind) -> int:
    with socket.socket(socket.AF_INET, kind) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@contextlib.contextmanager
def livekit_server() -> Iterator[LiveKitServer]:
    """Run ``livekit-server`` in Docker for the duration of the block."""
    docker = shutil.which("docker") or "docker"
    port, tcp, udp = (
        _free_port(socket.SOCK_STREAM),
        _free_port(socket.SOCK_STREAM),
        _free_port(socket.SOCK_DGRAM),
    )
    config = (
        f"port: {port}\n"
        f"rtc:\n  tcp_port: {tcp}\n  udp_port: {udp}\n  use_external_ip: false\n"
        f"keys:\n  {API_KEY}: {API_SECRET}\n"
        "logging:\n  level: warn\n"
    )
    name = f"hecklr-livekit-{port}"
    command = [
        docker, "run", "--rm", "-d", "--name", name,
        "-p", f"127.0.0.1:{port}:{port}", "-p", f"127.0.0.1:{tcp}:{tcp}",
        "-p", f"127.0.0.1:{udp}:{udp}/udp",
        # Inside the container the server must listen on every interface to be published;
        # the host only exposes it on 127.0.0.1.
        IMAGE, "--bind", "0.0.0.0", "--node-ip", "127.0.0.1", "--config-body", config,  # noqa: S104
    ]  # fmt: skip
    started = subprocess.run(command, capture_output=True, text=True, check=False, timeout=300)  # noqa: S603 -- fixed arguments
    if started.returncode != 0:
        raise RuntimeError(f"cannot start livekit-server: {started.stderr.strip()}")
    server = LiveKitServer(port)
    try:
        _wait_ready(server)
        yield server
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, check=False, timeout=60)  # noqa: S603 -- fixed arguments


def _wait_ready(server: LiveKitServer) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with contextlib.suppress(httpx2.TransportError):
            if httpx2.get(server.http_url, timeout=1).status_code == 200:
                return
        time.sleep(0.2)
    raise RuntimeError("livekit-server did not become ready in 30 s")


@dataclass
class LiveKitAgent:
    """The reference agent worker and the calls it took."""

    server: LiveKitServer
    prefix: str
    make_brain: Callable[[], LoopbackBrain]
    rhythm: Rhythm = "burst"
    calls: list[CallLog] = field(default_factory=list)
    _joined: set[str] = field(default_factory=set)

    async def run(self) -> None:
        """Join every new room with this agent's prefix, until cancelled."""
        async with (
            api.LiveKitAPI(self.server.http_url, API_KEY, API_SECRET) as lk,
            anyio.create_task_group() as group,
        ):
            while True:
                rooms = await lk.room.list_rooms(api.ListRoomsRequest())
                for room in rooms.rooms:
                    if room.name.startswith(self.prefix) and room.name not in self._joined:
                        self._joined.add(room.name)
                        group.start_soon(self._answer, room.name)
                await anyio.sleep(0.05)

    async def _answer(self, room_name: str) -> None:
        room = rtc.Room()
        token = (
            api.AccessToken(API_KEY, API_SECRET)
            .with_identity("agent")
            .with_grants(api.VideoGrants(room_join=True, room=room_name))
            .to_jwt()
        )
        await room.connect(self.server.url, token)
        caller = await _first_caller(room)
        log = CallLog(self.make_brain(), caller.attributes.get("hecklr.trial_id"))
        log.brain.media_clock = False
        self.calls.append(log)
        left = anyio.Event()

        def on_left(participant: rtc.RemoteParticipant) -> None:
            if participant.identity == caller.identity:
                log.caller_hung_up = True
                left.set()

        room.on("participant_disconnected", on_left)
        source = rtc.AudioSource(AGENT_RATE, 1, queue_size_ms=20_000)
        track = rtc.LocalAudioTrack.create_audio_track("agent-voice", source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )

        async def send(pcm: Pcm16) -> None:
            step = AGENT_RATE // 100
            for start in range(0, len(pcm), step):
                chunk = pcm[start : start + step]
                frame = rtc.AudioFrame(chunk.tobytes(), AGENT_RATE, 1, len(chunk))
                await source.capture_frame(frame)

        async def clear() -> None:
            source.clear_queue()

        async with anyio.create_task_group() as group:
            group.start_soon(_listen, caller, log.brain)
            group.start_soon(_cancel_on, left, group.cancel_scope)
            await speak(log.brain, Voice(self.rhythm, AGENT_RATE, send, clear))
            group.cancel_scope.cancel()
        await room.disconnect()
        await source.aclose()


async def _cancel_on(event: anyio.Event, scope: anyio.CancelScope) -> None:
    await event.wait()
    scope.cancel()


async def _first_caller(room: rtc.Room) -> rtc.RemoteParticipant:
    with anyio.fail_after(10):
        while not room.remote_participants:  # noqa: ASYNC110 -- LiveKit reports joins by callback
            await anyio.sleep(0.01)
    return next(iter(room.remote_participants.values()))


async def _listen(caller: rtc.RemoteParticipant, brain: LoopbackBrain) -> None:
    with anyio.fail_after(10):
        while not [pub for pub in caller.track_publications.values() if pub.track]:  # noqa: ASYNC110 -- LiveKit reports subscriptions by callback
            await anyio.sleep(0.01)
    track = next(pub.track for pub in caller.track_publications.values() if pub.track)
    stream = rtc.AudioStream.from_track(track=track, sample_rate=AGENT_RATE, num_channels=1)
    async for event in stream:
        samples = np.frombuffer(event.frame.data, dtype=np.int16).copy()
        brain.hear(samples, AGENT_RATE, now_ms())


@contextlib.contextmanager
def run_livekit_agent(agent: LiveKitAgent) -> Iterator[None]:
    """Run the agent worker on its own loop for the duration of the block."""
    thread = AgentThread()
    task = asyncio.run_coroutine_threadsafe(agent.run(), thread.loop)
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(concurrent.futures.CancelledError):
            task.result(timeout=10)
        thread.stop()
