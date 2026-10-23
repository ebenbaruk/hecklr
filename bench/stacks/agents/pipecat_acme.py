"""The acme brain inside a stock Pipecat 1.x pipeline, served over SmallWebRTC.

Everything around the brain is Pipecat's own, at its defaults: Silero VAD and the smart-turn v3
end-of-turn model on the user aggregator, interruptions on, and per call::

    transport.input() -> STT -> user aggregator -> acme brain -> TTS -> transport.output()
                      -> assistant aggregator

The acme brain takes the place of the LLM service: it answers each finished user turn
(``LLMContextFrame``) with the acme rules brain, the same one every voice stack of the benchmark
runs, so rows differ only by their speech stack. A reply that hands the caller to a human ends
the call once it has been said (``EndWorkerFrame``).

    uv run python bench/stacks/agents/pipecat_acme.py --stt whisper --tts kokoro --port 7860
    uv run python bench/stacks/agents/pipecat_acme.py --stt assemblyai --tts openai --port 7860

``POST /api/offer`` takes a SmallWebRTC offer (the ``pipecat`` adapter of Hecklr); the trial's
world comes from the ``X-Hecklr-World-Url`` header. ``GET /healthz`` reports the calls taken and
what the cloud services cost so far (``spent_usd``), metered at list price: AssemblyAI
Universal-Streaming bills session time, OpenAI's ``gpt-4o-mini-tts`` about USD 0.015 per minute
of speech (October 2026 price pages). Local services cost nothing.
"""

import _paths  # noqa: F401, I001 -- puts the reference agents on sys.path; must come first

import argparse
import asyncio
import dataclasses
import os
import sys
import time
import warnings
from collections import Counter
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from typing import Any, Final, Literal

import httpx2
import numpy as np
import uvicorn
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
from acme_voice import GREETING, normalize_transcript
from faster_whisper import WhisperModel  # type: ignore[import-untyped]  # ships no type hints
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from loguru import logger

from hecklr_adapters import WORLD_URL_HEADER
from hecklr_schemas import TRIAL_ID_HEADER, World, new_id
from hecklr_world import load_world

# Pipecat's audio helpers still import the stdlib ``audioop``, deprecated in Python 3.12.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.frames.frames import (
        EndWorkerFrame,
        Frame,
        LLMContextFrame,
        LLMFullResponseEndFrame,
        LLMFullResponseStartFrame,
        LLMTextFrame,
        TranscriptionFrame,
        TTSAudioRawFrame,
        TTSSpeakFrame,
    )
    from pipecat.pipeline.pipeline import Pipeline
    from pipecat.pipeline.worker import PipelineParams, PipelineWorker
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import (
        LLMContextAggregatorPair,
        LLMUserAggregatorParams,
    )
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
    from pipecat.services.assemblyai.stt import AssemblyAISTTService
    from pipecat.services.kokoro.tts import KokoroTTSService
    from pipecat.services.openai.tts import OpenAITTSService
    from pipecat.services.settings import STTSettings
    from pipecat.services.stt_service import SegmentedSTTService, STTService
    from pipecat.services.tts_service import TTSService
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.connection import SmallWebRTCConnection
    from pipecat.transports.smallwebrtc.request_handler import (
        SmallWebRTCRequest,
        SmallWebRTCRequestHandler,
    )
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport
    from pipecat.utils.time import time_now_iso8601
    from pipecat.workers.runner import WorkerRunner

Stt = Literal["whisper", "assemblyai"]
Tts = Literal["kokoro", "openai"]
IN_RATE: Final = 16_000
OUT_RATE: Final = 24_000
"""Kokoro and OpenAI both synthesize at 24 kHz: no resampling on the way out."""
WHISPER_MODEL: Final = "base.en"
"""The recognizer of the acme-voice reference pipeline, for a like-for-like comparison."""
KOKORO_VOICE: Final = "af_heart"
OPENAI_VOICE: Final = "alloy"
ASSEMBLYAI_USD_PER_HOUR: Final = Decimal("0.15")
OPENAI_TTS_USD_PER_MINUTE: Final = Decimal("0.015")


@dataclass
class Meter:
    """Cloud usage at list price, and the calls taken."""

    stt: Stt
    tts: Tts
    cap_usd: Decimal | None = None
    """Refuse new calls once the cloud spend reaches this; ``None``: no cap."""
    stt_seconds: float = 0.0
    """Streaming session time of finished calls."""
    tts_seconds: float = 0.0
    live_since: dict[int, float] = field(default_factory=dict)
    """Start (monotonic seconds) of each call in progress, by call number: a session bills while
    it is open, so the spend read when a conversation ends already includes it."""
    calls: int = 0
    calls_by_world_source: Counter[str] = field(default_factory=Counter)

    @property
    def spent_usd(self) -> Decimal:
        """What the cloud services cost so far; local services are free."""
        spent = Decimal(0)
        if self.stt == "assemblyai":
            now = time.monotonic()
            seconds = self.stt_seconds + sum(now - start for start in self.live_since.values())
            spent += ASSEMBLYAI_USD_PER_HOUR * Decimal(str(seconds)) / 3600
        if self.tts == "openai":
            spent += OPENAI_TTS_USD_PER_MINUTE * Decimal(str(self.tts_seconds)) / 60
        return spent.quantize(Decimal("0.000001"))

    @property
    def exhausted(self) -> bool:
        """Whether the cap is reached (a call in progress may still exceed it by one call)."""
        return self.cap_usd is not None and self.spent_usd >= self.cap_usd


class AcmeBrain(FrameProcessor):
    """Answers each finished user turn with the acme brain, in place of an LLM service."""

    def __init__(self, agent: AcmeAgent, backend: ToolBackend) -> None:
        super().__init__()
        self._agent = agent
        self._backend = backend
        self._session = new_id()

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Answer user turns; pass every other frame on."""
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame):
            # A speculative context may still change; only a confirmed turn is answered.
            if not frame.speculation:
                await self._answer(frame.context)
            return
        await self.push_frame(frame, direction)

    async def _answer(self, context: LLMContext) -> None:
        text = normalize_transcript(last_user_text(context.get_messages()))
        if not text:
            return
        reply = await self._agent.respond(self._session, text, lambda: self._backend)
        await self.push_frame(LLMFullResponseStartFrame())
        await self.push_frame(LLMTextFrame(reply.text))
        await self.push_frame(LLMFullResponseEndFrame())
        if reply.ended:
            await self.push_frame(EndWorkerFrame())


def last_user_text(messages: Sequence[Any]) -> str:
    """The text of the newest user message (content as a string or a list of text parts)."""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                part["text"] for part in content if isinstance(part, dict) and "text" in part
            )
    return ""


class SpeechMeter(FrameProcessor):
    """Counts the seconds of speech the TTS produced."""

    def __init__(self, meter: Meter) -> None:
        super().__init__()
        self._meter = meter

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        """Count TTS audio, pass everything on."""
        await super().process_frame(frame, direction)
        if isinstance(frame, TTSAudioRawFrame):
            self._meter.tts_seconds += len(frame.audio) / 2 / frame.sample_rate
        await self.push_frame(frame, direction)


@lru_cache(maxsize=1)
def whisper_model() -> Any:  # faster-whisper is untyped
    """faster-whisper ``base.en`` on CPU, loaded once per process and shared by every call."""
    return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")


class FasterWhisperSTT(SegmentedSTTService):
    """Pipecat's segmented recognizer contract over faster-whisper.

    Pipecat 1.13's own ``WhisperSTTService`` cannot be imported on macOS without the optional
    ``mlx-whisper`` package (its module imports it at load time), so this stack decodes the way
    that service does (one segment per VAD-bounded utterance, in a worker thread) with the model
    the acme-voice reference pipeline uses.
    """

    def __init__(self) -> None:
        super().__init__(settings=STTSettings(model=WHISPER_MODEL, language="en"))

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        """Transcribe one utterance of 16-bit PCM at the pipeline's input rate."""
        samples = np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0

        def transcribe() -> str:
            segments, _ = whisper_model().transcribe(samples, language="en", beam_size=1)
            return " ".join(segment.text.strip() for segment in segments).strip()

        text = await asyncio.to_thread(transcribe)
        if text:
            yield TranscriptionFrame(text, self._user_id, time_now_iso8601())


def make_stt(kind: Stt) -> STTService:
    """The recognizer of that kind."""
    if kind == "assemblyai":
        return AssemblyAISTTService(api_key=os.environ["ASSEMBLYAI_API_KEY"])
    return FasterWhisperSTT()


def make_tts(kind: Tts) -> TTSService:
    """Pipecat's speech synthesizer of that kind."""
    if kind == "openai":
        return OpenAITTSService(
            api_key=os.environ["OPENAI_API_KEY"],
            settings=OpenAITTSService.Settings(voice=OPENAI_VOICE),
        )
    return KokoroTTSService(settings=KokoroTTSService.Settings(voice=KOKORO_VOICE))


@dataclass
class AcmeBot:
    """Takes calls: one pipeline per SmallWebRTC connection."""

    agent: AcmeAgent
    world: World
    client: httpx2.AsyncClient
    meter: Meter

    def backend(self, world_url: str | None, trial_id: str | None) -> ToolBackend:
        """The trial's world sandbox, or a private copy of the world for a manual call."""
        source = "header" if world_url else "local"
        self.meter.calls_by_world_source[source] += 1
        if world_url:
            return HttpWorldTools(self.client, world_url, trial_id)
        return LocalWorldTools(self.world)

    async def run(self, connection: SmallWebRTCConnection, backend: ToolBackend) -> None:
        """One call, until the caller hangs up or the agent hands the call over."""
        self.meter.calls += 1
        transport = SmallWebRTCTransport(
            webrtc_connection=connection,
            params=TransportParams(audio_in_enabled=True, audio_out_enabled=True),
        )
        user, assistant = LLMContextAggregatorPair(
            LLMContext(), user_params=LLMUserAggregatorParams(vad_analyzer=SileroVADAnalyzer())
        )
        pipeline = Pipeline(
            [
                transport.input(),
                make_stt(self.meter.stt),
                user,
                AcmeBrain(self.agent, backend),
                make_tts(self.meter.tts),
                SpeechMeter(self.meter),
                transport.output(),
                assistant,
            ]
        )
        worker = PipelineWorker(
            pipeline,
            params=PipelineParams(audio_in_sample_rate=IN_RATE, audio_out_sample_rate=OUT_RATE),
        )
        runner = WorkerRunner(handle_sigint=False)

        @transport.event_handler("on_client_connected")  # type: ignore[untyped-decorator]
        async def on_connected(transport: Any, client: Any) -> None:
            await worker.queue_frames([TTSSpeakFrame(GREETING)])

        @transport.event_handler("on_client_disconnected")  # type: ignore[untyped-decorator]
        async def on_disconnected(transport: Any, client: Any) -> None:
            await runner.cancel()

        call = self.meter.calls
        self.meter.live_since[call] = time.monotonic()
        try:
            await runner.add_workers(worker)
            await runner.run()
        finally:
            # A streaming recognizer's session lasts as long as the call.
            self.meter.stt_seconds += time.monotonic() - self.meter.live_since.pop(call)


def build_app(bot: AcmeBot) -> FastAPI:
    """Pipecat's runner endpoint plus a health route."""
    app = FastAPI()
    handler = SmallWebRTCRequestHandler()

    @app.post("/api/offer")
    async def offer(request: Request, background: BackgroundTasks) -> dict[str, str] | None:
        if bot.meter.exhausted:
            raise HTTPException(503, f"the agent spent its ${bot.meter.cap_usd} cap")
        backend = bot.backend(
            request.headers.get(WORLD_URL_HEADER), request.headers.get(TRIAL_ID_HEADER)
        )
        body = SmallWebRTCRequest.from_dict(await request.json())

        async def started(connection: SmallWebRTCConnection) -> None:
            background.add_task(bot.run, connection, backend)

        answer: dict[str, str] | None = await handler.handle_web_request(body, started)
        return answer

    @app.get("/healthz")
    async def healthz() -> dict[str, object]:
        meter = bot.meter
        return {
            "status": "ok",
            "stt": meter.stt,
            "tts": meter.tts,
            "calls_total": meter.calls,
            "calls_by_world_source": dict(meter.calls_by_world_source),
            "spent_usd": str(meter.spent_usd),
        }

    return app


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(prog="pipecat_acme", description=__doc__.splitlines()[0])
    parser.add_argument("--stt", choices=["whisper", "assemblyai"], default="whisper")
    parser.add_argument("--tts", choices=["kokoro", "openai"], default="kokoro")
    parser.add_argument("--budget-usd", type=Decimal, help="cap on the cloud services' spend")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    return parser.parse_args(argv)


def warm_up(stt: Stt, tts: Tts) -> None:
    """Download and load the local models once, before the first call needs them."""
    if stt == "whisper":
        whisper_model()
    if tts == "kokoro":
        make_tts(tts)


async def serve(args: argparse.Namespace) -> None:
    """Serve calls until the process is stopped."""
    settings = dataclasses.replace(AgentSettings.from_env(), brain="rules")
    async with httpx2.AsyncClient(timeout=settings.tool_timeout_s) as client:
        bot = AcmeBot(
            AcmeAgent(load_agent_files(), settings),
            load_world(PROJECT_ROOT / WORLD_FILE),
            client,
            Meter(args.stt, args.tts, args.budget_usd),
        )
        config = uvicorn.Config(build_app(bot), host=args.host, port=args.port, log_level="warning")
        await uvicorn.Server(config).serve()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point."""
    args = parse_args(argv)
    logger.remove()  # Pipecat logs every frame-level step at DEBUG; keep warnings only
    logger.add(sys.stderr, level="WARNING")
    for key in (["ASSEMBLYAI_API_KEY"] if args.stt == "assemblyai" else []) + (
        ["OPENAI_API_KEY"] if args.tts == "openai" else []
    ):
        if not os.environ.get(key):
            sys.stderr.write(f"pipecat_acme: {key} is not set\n")
            return 2
    warm_up(args.stt, args.tts)
    asyncio.run(serve(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
