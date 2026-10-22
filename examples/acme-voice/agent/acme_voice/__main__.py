"""Command line: start the acme voice agent's WebSocket server.

    uv run python examples/acme-voice/agent/serve.py --brain rules --port 8765
    uv run python examples/acme-voice/agent/serve.py --brain rules --tts synthetic --fault F7

``--tts synthetic`` speaks tagged synthetic audio, which Hecklr's free ``fake/tag`` ASR hears
exactly; the agent still hears callers with the ASR of ``voice.yaml``.

It prints ``listening on ws://<host>:<port>/`` once models are loaded and calls are accepted.
"""

import argparse
import dataclasses
import gc
import signal
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final

import anyio
import httpx2
import numpy as np
from acme_agent import (
    PROJECT_ROOT as CHAT_ROOT,
)
from acme_agent import (
    WORLD_FILE,
    AcmeAgent,
    AgentConfigError,
    AgentSettings,
    load_agent_files,
)

from acme_voice.config import (
    PROJECT_ROOT,
    PipelineConfig,
    VoiceConfigError,
    load_pipeline_config,
    voice_fault_overlay,
)
from acme_voice.pipeline import SpeechStack
from acme_voice.server import VoiceAgentServer, bound_port, running_server
from hecklr_llm import ProviderRouter
from hecklr_voice import (
    EnergyVad,
    SileroVad,
    SpeechRequest,
    VoiceProviderError,
    make_asr,
    make_tts,
    warm_dsp_pool,
)
from hecklr_voice import (
    VoiceConfigError as EngineConfigError,
)
from hecklr_voice.pool import dsp_workers, run_dsp
from hecklr_voice.vad import VadFactory
from hecklr_world import WorldConfigError, load_world

DEFAULT_MODELS_DIR = PROJECT_ROOT / ".hecklr" / "models"
KEEP_WARM_S: Final = 60.0
"""How often every speech worker gets a moment's work (anyio retires workers idle 300 s)."""
WARM_JOB_S: Final = 0.05
"""Length of a keep-warm job: long enough that the jobs overlap and land on distinct workers."""


@dataclass(frozen=True, slots=True)
class Options:
    """What the command line asks for."""

    agent: AgentSettings
    pipeline: PipelineConfig
    host: str
    port: int
    models_dir: Path
    world_base_url: str | None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The command line flags."""
    parser = argparse.ArgumentParser(prog="acme-voice", description=__doc__.splitlines()[0])
    parser.add_argument("--brain", choices=["llm", "rules"], help="default: llm (ACME_BRAIN)")
    parser.add_argument("--model", help="LLM brain model, provider/model (ACME_MODEL)")
    variant = parser.add_mutually_exclusive_group()
    variant.add_argument("--fault", help="voice fault variant, F7..F10")
    variant.add_argument("--overlay", type=Path, help="overlay directory holding a voice.yaml")
    parser.add_argument("--vad", choices=["energy", "silero"], help="override voice.yaml's vad")
    parser.add_argument("--asr", help="override voice.yaml's asr_model, e.g. fake/tag")
    parser.add_argument("--tts", choices=["kokoro", "synthetic"], help="override tts_provider")
    parser.add_argument("--world-base-url", help="Hecklr world sandbox root URL")
    parser.add_argument("--models-dir", type=Path, default=DEFAULT_MODELS_DIR)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    return parser.parse_args(argv)


def options_from(args: argparse.Namespace) -> Options:
    """Settings for the agent and its pipeline.

    The brain's settings come from the ``ACME_*`` environment variables (``AgentSettings``),
    with ``--brain`` and ``--model`` on top, so ``ACME_FAULT=F3`` runs a brain fault (F1-F6) over
    voice while ``--fault`` picks a voice fault (F7-F10).

    Raises:
        VoiceConfigError, AgentConfigError: an invalid flag or file.
    """
    overlay = args.overlay or (voice_fault_overlay(args.fault) if args.fault else None)
    pipeline = load_pipeline_config(overlay=overlay).with_overrides(
        vad=args.vad, asr_model=args.asr, tts_provider=args.tts
    )
    overrides = {"brain": args.brain, "model": args.model}
    agent = dataclasses.replace(
        AgentSettings.from_env(), **{key: value for key, value in overrides.items() if value}
    )
    return Options(agent, pipeline, args.host, args.port, args.models_dir, args.world_base_url)


def speech_stack(pipeline: PipelineConfig, models_dir: Path) -> SpeechStack:
    """The ASR, TTS and VAD ``voice.yaml`` names (TTS cached on disk next to the models)."""
    vad: VadFactory = EnergyVad
    if pipeline.vad == "silero":
        vad = partial(SileroVad, models_dir=models_dir)
    return SpeechStack(
        asr=make_asr(pipeline.asr_model, models_dir=models_dir),
        tts=make_tts(
            pipeline.tts_provider, cache_dir=models_dir.parent / "agent-tts", models_dir=models_dir
        ),
        vad=vad,
        voice=pipeline.tts_voice,
    )


async def warm_up(stack: SpeechStack) -> None:
    """Load every model in every DSP worker now, so the first call is not the slow one."""
    await warm_dsp_pool()
    await stack.vad().prepare()
    silence = np.zeros(16_000, dtype=np.int16)

    async def one(index: int) -> None:
        await stack.asr.transcribe(silence, language=stack.language)
        await stack.tts.synthesize(SpeechRequest(text=f"Warming up {index}.", voice=stack.voice))

    async with anyio.create_task_group() as group:
        for index in range(dsp_workers()):
            group.start_soon(one, index)


async def keep_warm(interval_s: float = KEEP_WARM_S) -> None:
    """Give every DSP worker a moment's work every ``interval_s``, forever.

    anyio retires a worker that has been idle for five minutes, and reuses idle workers last in,
    first out, so the workers warmed at start-up are the ones that sit idle and get retired.
    The next peak of calls then starts a worker that loads the speech models on a caller's
    answer path: 4-7 s, measured five minutes into every timing suite run.
    """
    while True:
        await anyio.sleep(interval_s)
        async with anyio.create_task_group() as group:
            for _ in range(dsp_workers()):
                group.start_soon(run_dsp, time.sleep, WARM_JOB_S)


async def serve(options: Options) -> None:
    """Serve calls until SIGINT or SIGTERM."""
    world = load_world(CHAT_ROOT / WORLD_FILE)
    stack = speech_stack(options.pipeline, options.models_dir)
    await warm_up(stack)
    async with (
        ProviderRouter() as llm,
        httpx2.AsyncClient(timeout=options.agent.tool_timeout_s) as client,
    ):
        agent = AcmeAgent(
            load_agent_files(overlay=options.agent.overlay_dir()),
            options.agent,
            llm if options.agent.brain == "llm" else None,
        )
        server = VoiceAgentServer(
            agent, world, options.pipeline, stack, client, world_base_url=options.world_base_url
        )
        async with running_server(server, options.host, options.port) as running:
            # Keep start-up objects out of the collector's way: a full collection pauses the
            # loop, and with it every call's audio (ADR 0009a).
            gc.collect()
            gc.freeze()
            print(f"listening on ws://{options.host}:{bound_port(running)}/", flush=True)  # noqa: T201 -- the CLI's output
            async with anyio.create_task_group() as group:
                group.start_soon(keep_warm)
                with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
                    async for _ in signals:
                        group.cancel_scope.cancel()
                        return


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code (2 for a configuration error)."""
    try:
        options = options_from(parse_args(argv))
        anyio.run(serve, options)
    except (
        AgentConfigError,
        VoiceConfigError,
        EngineConfigError,
        VoiceProviderError,
        WorldConfigError,
    ) as exc:
        sys.stderr.write(f"acme-voice: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
