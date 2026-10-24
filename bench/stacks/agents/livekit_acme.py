"""The acme-voice cascade behind LiveKit: a local ``livekit-server`` and an agent worker.

The call pipeline is the acme-voice reference one, unchanged (``acme_voice.VoiceCall``: Silero
VAD, 800 ms endpointing, faster-whisper base.en, the acme rules brain, Kokoro), so this stack
differs from ``acme-voice-local`` only by its transport: WebRTC through a LiveKit SFU instead of
raw PCM over a WebSocket.

The script starts ``livekit-server`` (the official Docker image) on the given ports, then works
like a LiveKit agent worker: it joins every new room whose name starts with ``--room-prefix`` as
``agent``, hears the caller's track at 16 kHz and speaks through an ``AudioSource``, clearing its
queue on a barge-in. The trial's world comes from the caller's ``hecklr.world_url`` attribute
(set by Hecklr's ``livekit`` adapter). It prints ``listening on ws://...`` once calls can be
taken, and removes its container when stopped (SIGTERM or SIGINT).

    export HECKLR_BENCH_LIVEKIT_KEY=k HECKLR_BENCH_LIVEKIT_SECRET=s
    uv run python bench/stacks/agents/livekit_acme.py --port 7880 --tcp-port 7881 --udp-port 7882
"""

import _paths  # noqa: F401, I001 -- puts the reference agents on sys.path; must come first

import argparse
import dataclasses
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import contextmanager
from typing import Final
from collections.abc import Iterator

import anyio
import httpx2
import numpy as np
from acme_agent import (
    PROJECT_ROOT,
    WORLD_FILE,
    AcmeAgent,
    AgentSettings,
    HttpWorldTools,
    LocalWorldTools,
    ToolBackend,
    load_agent_files,
)
from acme_voice import SpeechStack, VoiceCall, load_pipeline_config
from acme_voice.__main__ import DEFAULT_MODELS_DIR, speech_stack, warm_up
from livekit import api, rtc

from hecklr_schemas import World, new_id
from hecklr_voice.audio import SAMPLE_RATE, Pcm16
from hecklr_world import load_world

IMAGE: Final = "livekit/livekit-server:v1.13.10"
KEY_ENV: Final = "HECKLR_BENCH_LIVEKIT_KEY"
SECRET_ENV: Final = "HECKLR_BENCH_LIVEKIT_SECRET"  # noqa: S105 -- names the variable only
FRAME_SAMPLES: Final = SAMPLE_RATE // 100
"""10 ms frames, LiveKit's native frame size."""
POLL_S: Final = 0.05


def now_ms() -> float:
    """The agent's clock."""
    return time.monotonic_ns() / 1e6


@contextmanager
def livekit_server(port: int, tcp_port: int, udp_port: int, key: str, secret: str) -> Iterator[str]:
    """``livekit-server`` in Docker on 127.0.0.1 for the block; yields its signal URL."""
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("livekit_acme: docker is not installed")
    config = (
        f"port: {port}\n"
        f"rtc:\n  tcp_port: {tcp_port}\n  udp_port: {udp_port}\n  use_external_ip: false\n"
        f"keys:\n  {key}: {secret}\n"
        "logging:\n  level: warn\n"
    )
    name = f"hecklr-bench-livekit-{port}"
    command = [
        docker, "run", "--rm", "-d", "--name", name,
        "-p", f"127.0.0.1:{port}:{port}", "-p", f"127.0.0.1:{tcp_port}:{tcp_port}",
        "-p", f"127.0.0.1:{udp_port}:{udp_port}/udp",
        # Inside the container the server listens on every interface so Docker can publish it;
        # the host exposes it on 127.0.0.1 only.
        IMAGE, "--bind", "0.0.0.0", "--node-ip", "127.0.0.1", "--config-body", config,  # noqa: S104
    ]  # fmt: skip
    started = subprocess.run(command, capture_output=True, text=True, check=False, timeout=300)  # noqa: S603 -- fixed arguments
    if started.returncode != 0:
        raise SystemExit(f"livekit_acme: cannot start livekit-server: {started.stderr.strip()}")
    try:
        _wait_ready(f"http://127.0.0.1:{port}")
        yield f"ws://127.0.0.1:{port}"
    finally:
        subprocess.run([docker, "rm", "-f", name], capture_output=True, check=False, timeout=60)  # noqa: S603 -- fixed arguments


def _wait_ready(url: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            if httpx2.get(url, timeout=1).status_code == 200:
                return
        except httpx2.TransportError:
            pass  # not listening yet
        time.sleep(0.2)
    raise SystemExit("livekit_acme: livekit-server did not become ready in 60 s")


class LiveKitMouth:
    """Speech out through the agent's published track."""

    def __init__(self, source: rtc.AudioSource, room: rtc.Room) -> None:
        self._source = source
        self._room = room

    async def play(self, pcm: Pcm16) -> None:
        """Queue the clip; the source plays it out in real time."""
        for start in range(0, len(pcm), FRAME_SAMPLES):
            chunk = pcm[start : start + FRAME_SAMPLES]
            frame = rtc.AudioFrame(chunk.tobytes(), SAMPLE_RATE, 1, len(chunk))
            await self._source.capture_frame(frame)

    async def clear(self) -> None:
        """Drop what the caller has not heard yet."""
        self._source.clear_queue()

    async def hang_up(self) -> None:
        """Leave the room: the caller sees the agent go."""
        await self._room.disconnect()


@dataclasses.dataclass
class Worker:
    """Joins each new benchmark room and runs one acme call in it."""

    url: str
    key: str
    secret: str
    prefix: str
    agent: AcmeAgent
    world: World
    stack: SpeechStack
    client: httpx2.AsyncClient
    joined: set[str] = dataclasses.field(default_factory=set)

    async def run(self) -> None:
        """Watch for rooms until cancelled."""
        http_url = self.url.replace("ws://", "http://")
        async with (
            api.LiveKitAPI(http_url, self.key, self.secret) as lk,
            anyio.create_task_group() as group,
        ):
            while True:
                rooms = await lk.room.list_rooms(api.ListRoomsRequest())
                for room in rooms.rooms:
                    if room.name.startswith(self.prefix) and room.name not in self.joined:
                        self.joined.add(room.name)
                        group.start_soon(self._answer, room.name)
                await anyio.sleep(POLL_S)

    async def _answer(self, room_name: str) -> None:
        room = rtc.Room()
        token = (
            api.AccessToken(self.key, self.secret)
            .with_identity("agent")
            .with_grants(api.VideoGrants(room_join=True, room=room_name))
            .to_jwt()
        )
        await room.connect(self.url, token)
        try:
            await self._converse(room)
        finally:
            with anyio.move_on_after(5, shield=True):
                await room.disconnect()

    async def _converse(self, room: rtc.Room) -> None:
        caller = await _first_caller(room)
        source = rtc.AudioSource(SAMPLE_RATE, 1, queue_size_ms=60_000)
        track = rtc.LocalAudioTrack.create_audio_track("agent-voice", source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        backend = self._backend(caller.attributes)
        session = new_id()
        call = VoiceCall(
            load_pipeline_config(),
            self.stack,
            lambda text: self.agent.respond(session, text, lambda: backend),
            LiveKitMouth(source, room),
            now_ms,
        )
        await call.prepare()
        left = anyio.Event()

        def on_left(participant: rtc.RemoteParticipant) -> None:
            if participant.identity == caller.identity:
                left.set()

        room.on("participant_disconnected", on_left)
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(_listen, caller, call)
                group.start_soon(_cancel_on, left, group.cancel_scope)
                await call.run()
                group.cancel_scope.cancel()
        finally:
            with anyio.move_on_after(5, shield=True):
                await source.aclose()

    def _backend(self, attributes: dict[str, str]) -> ToolBackend:
        world_url = attributes.get("hecklr.world_url")
        if world_url:
            return HttpWorldTools(self.client, world_url, attributes.get("hecklr.trial_id"))
        return LocalWorldTools(self.world)


async def _cancel_on(event: anyio.Event, scope: anyio.CancelScope) -> None:
    await event.wait()
    scope.cancel()


async def _first_caller(room: rtc.Room) -> rtc.RemoteParticipant:
    with anyio.fail_after(10):
        while not room.remote_participants:  # noqa: ASYNC110 -- LiveKit reports joins by callback
            await anyio.sleep(0.01)
    return next(iter(room.remote_participants.values()))


async def _listen(caller: rtc.RemoteParticipant, call: VoiceCall) -> None:
    with anyio.fail_after(10):
        while not [p for p in caller.track_publications.values() if p.track]:  # noqa: ASYNC110 -- LiveKit reports subscriptions by callback
            await anyio.sleep(0.01)
    track = next(p.track for p in caller.track_publications.values() if p.track)
    stream = rtc.AudioStream.from_track(track=track, sample_rate=SAMPLE_RATE, num_channels=1)
    try:
        async for event in stream:
            await call.hear(np.frombuffer(event.frame.data, dtype=np.int16).copy())
    finally:
        with anyio.move_on_after(5, shield=True):
            await stream.aclose()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(prog="livekit_acme", description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True, help="LiveKit signal port")
    parser.add_argument("--tcp-port", type=int, required=True, help="WebRTC over TCP")
    parser.add_argument("--udp-port", type=int, required=True, help="WebRTC over UDP")
    parser.add_argument("--room-prefix", default="bench")
    return parser.parse_args(argv)


async def serve(args: argparse.Namespace, url: str, key: str, secret: str) -> None:
    """Take calls until SIGINT or SIGTERM."""
    stack = speech_stack(load_pipeline_config(), DEFAULT_MODELS_DIR)
    await warm_up(stack)
    settings = dataclasses.replace(AgentSettings.from_env(), brain="rules")
    async with httpx2.AsyncClient(timeout=settings.tool_timeout_s) as client:
        worker = Worker(
            url, key, secret, args.room_prefix,
            AcmeAgent(load_agent_files(), settings),
            load_world(PROJECT_ROOT / WORLD_FILE),
            stack,
            client,
        )  # fmt: skip
        print(f"listening on {url}", flush=True)  # noqa: T201 -- the readiness line the runner waits for
        async with anyio.create_task_group() as group:
            group.start_soon(worker.run)
            with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
                async for _ in signals:
                    group.cancel_scope.cancel()
                    return


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; exit status 2 when the credentials are not set."""
    args = parse_args(argv)
    key, secret = os.environ.get(KEY_ENV), os.environ.get(SECRET_ENV)
    if not key or not secret:
        sys.stderr.write(f"livekit_acme: set {KEY_ENV} and {SECRET_ENV}\n")
        return 2
    with livekit_server(args.port, args.tcp_port, args.udp_port, key, secret) as url:
        anyio.run(serve, args, url, key, secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
