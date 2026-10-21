"""Pipecat side of the voice contract: a real Pipecat pipeline behind SmallWebRTC.

The reference agent is what Pipecat's runner serves: a FastAPI ``POST /api/offer`` handled by
``SmallWebRTCRequestHandler``, and per connection a pipeline
``transport.input() -> LoopbackProcessor -> transport.output()``. The processor feeds the
caller's ``InputAudioRawFrame`` s to the loopback brain and speaks its replies as
``OutputAudioRawFrame`` s, interrupting with ``broadcast_interruption()``, which makes Pipecat's
output transport drop queued audio, as a real Pipecat bot's interruption does. It runs on its
own loop in a background thread, like the other reference agents.
"""

import asyncio
import contextlib
import socket
import sys
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import uvicorn
from fastapi import BackgroundTasks, FastAPI, Request
from loguru import logger

from hecklr_schemas import TRIAL_ID_HEADER
from hecklr_voice.audio import Pcm16

from .voice_reference import CallLog, LoopbackBrain, Voice, now_ms, speak

# Pipecat's audio helpers still import the stdlib ``audioop``, deprecated in Python 3.12.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", "'audioop' is deprecated", DeprecationWarning)
    from pipecat.frames.frames import (
        EndWorkerFrame,
        Frame,
        InputAudioRawFrame,
        OutputAudioRawFrame,
        StartFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
    from pipecat.transports.smallwebrtc.request_handler import (
        SmallWebRTCRequest,
        SmallWebRTCRequestHandler,
    )
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
    from pipecat.workers.runner import WorkerRunner

logger.remove()  # Pipecat logs every frame-level step at DEBUG; keep warnings only
logger.add(sys.stderr, level="WARNING")

RATE = 48_000
"""WebRTC's Opus rate: the pipeline then never resamples, so the agent hears exact timing."""


class LoopbackProcessor(FrameProcessor):
    """The loopback brain inside a Pipecat pipeline."""

    def __init__(self, log: CallLog) -> None:
        super().__init__()
        self._log = log
        self._speaker: asyncio.Task[None] | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Hear caller audio; start speaking once the pipeline has started."""
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            samples = np.frombuffer(frame.audio, dtype=np.int16).copy()
            self._log.brain.hear(samples, frame.sample_rate, now_ms())
            return
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame) and self._speaker is None:
            self._speaker = asyncio.get_running_loop().create_task(self._speak())
            self._speaker.add_done_callback(_report_failure)

    async def _speak(self) -> None:
        async def send(pcm: Pcm16) -> None:
            for start in range(0, len(pcm), RATE // 50):
                chunk = pcm[start : start + RATE // 50]
                await self.push_frame(OutputAudioRawFrame(chunk.tobytes(), RATE, 1))

        await speak(self._log.brain, Voice("burst", RATE, send, self.broadcast_interruption))
        await self.push_frame(EndWorkerFrame())  # hang up: the transport closes the connection


def _report_failure(task: asyncio.Task[None]) -> None:
    """A reference agent that breaks must say so, not leave the test to time out silently."""
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.opt(exception=error).error("reference Pipecat agent failed")


@dataclass
class PipecatAgent:
    """The reference Pipecat bot and the calls it took."""

    make_brain: Callable[[], LoopbackBrain]
    calls: list[CallLog] = field(default_factory=list)

    async def run_bot(self, connection: SmallWebRTCConnection, trial_id: str | None) -> None:
        """One call: a pipeline on the connection until the caller leaves."""
        log = CallLog(self.make_brain(), trial_id)
        self.calls.append(log)
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        pipeline = Pipeline([transport.input(), LoopbackProcessor(log), transport.output()])
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(audio_in_sample_rate=RATE, audio_out_sample_rate=RATE),
        )
        runner = WorkerRunner(handle_sigint=False)

        @transport.event_handler("on_client_disconnected")  # type: ignore[untyped-decorator]
        async def on_disconnected(transport: Any, client: Any) -> None:
            log.caller_hung_up = True
            await runner.cancel()

        await runner.add_workers(worker)
        await runner.run()


def build_app(agent: PipecatAgent) -> FastAPI:
    """Pipecat's runner endpoint, reduced to what a call needs."""
    app = FastAPI()
    handler = SmallWebRTCRequestHandler()

    @app.post("/api/offer")
    async def offer(request: Request, background: BackgroundTasks) -> dict[str, str] | None:
        trial_id = request.headers.get(TRIAL_ID_HEADER)
        body = SmallWebRTCRequest.from_dict(await request.json())

        async def started(connection: SmallWebRTCConnection) -> None:
            background.add_task(agent.run_bot, connection, trial_id)

        answer: dict[str, str] | None = await handler.handle_web_request(body, started)
        return answer

    return app


@contextlib.contextmanager
def serve_pipecat(agent: PipecatAgent) -> Iterator[str]:
    """Serve the bot on an ephemeral 127.0.0.1 port in its own thread; yield the offer URL."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(build_app(agent), log_level="warning", lifespan="off"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("reference Pipecat agent did not start")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{sock.getsockname()[1]}/api/offer"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
