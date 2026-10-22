"""Graded voice trials against the acme voice agent, in process: ``hecklr run`` minus storage.

``run_voice_trial`` runs one scripted scenario as a real trial: the voice agent serves calls on
a localhost WebSocket (``pcm`` protocol), Hecklr's ``VoiceDriver`` calls it through the
``websocket`` adapter with the scenario's scripted caller, the agent's tools act on the trial's
world in a real ``hecklr_world.Sandbox`` over HTTP, and ``hecklr_grade`` grades the events and
the world. Callers speak ``synthetic`` speech here (no model download), so the agent hears the
script through ``ScriptedASR``; everything after the ASR is the real agent.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

import httpx2
from acme_agent import PROJECT_ROOT as CHAT_ROOT
from acme_agent import WORLD_FILE, AcmeAgent, AgentSettings, load_agent_files
from acme_voice import (
    PROJECT_ROOT,
    PipelineConfig,
    SpeechStack,
    VoiceAgentServer,
    bound_port,
    load_pipeline_config,
    running_server,
    voice_fault_overlay,
)
from voice_testing import ScriptedASR

from hecklr_adapters.voice import WebSocketVoiceAdapterConfig, WebSocketVoiceTarget
from hecklr_core.yaml_files import load_model
from hecklr_grade import build_graders, grade_trial, trial_passed
from hecklr_schemas import (
    Channel,
    Event,
    Grade,
    GradeContext,
    Persona,
    RunId,
    Scenario,
    SessionContext,
    Trial,
    TrialBrief,
    TrialId,
    TtsConfig,
    UtterancePayload,
    new_id,
)
from hecklr_sim import ScriptedSimulator
from hecklr_voice import EnergyVad, FakeASR, SyntheticTTS, VoiceDriver
from hecklr_voice.testing import RecordingSink
from hecklr_world import Sandbox, load_world

SEED: Final = 11
WORLD: Final = load_world(CHAT_ROOT / WORLD_FILE)


def variant(name: str) -> PipelineConfig:
    """The pipeline of ``clean`` or a voice fault (``F7``..``F9``) on the energy VAD."""
    overlay = None if name == "clean" else voice_fault_overlay(name)
    return replace(load_pipeline_config(overlay=overlay), vad="energy")


def scenario(path: str) -> Scenario:
    """A scenario of the acme-voice suite, by path under ``scenarios/``."""
    return load_model(PROJECT_ROOT / "scenarios" / path, Scenario)


def synthetic_persona(persona_id: str) -> Persona:
    """A persona of the suite speaking ``synthetic`` speech instead of Kokoro."""
    persona = load_model(PROJECT_ROOT / "personas" / f"{persona_id}.yaml", Persona)
    if persona.voice is None:
        raise ValueError(f"persona {persona_id!r} has no voice")
    tts = TtsConfig(
        provider="synthetic", voice=persona.voice.tts.voice, rate=persona.voice.tts.rate
    )
    return persona.model_copy(update={"voice": persona.voice.model_copy(update={"tts": tts})})


@dataclass(frozen=True)
class VoiceOutcome:
    """A graded voice trial."""

    trial: Trial
    events: list[Event]
    grades: list[Grade]

    @property
    def passed(self) -> bool:
        return trial_passed(self.grades)

    def transcript(self) -> str:
        """Who said what, in time order, for failure messages."""
        lines = [
            f"{event.t_ms:>6} {event.actor}: {event.payload.text}"
            for event in sorted(self.events, key=lambda event: (event.t_ms, event.seq))
            if isinstance(event.payload, UtterancePayload)
        ]
        return "\n".join(lines)

    def failed(self) -> set[str]:
        """Graders whose ``ok`` grade failed."""
        return {grade.grader for grade in self.grades if grade.status == "ok" and not grade.passed}


@asynccontextmanager
async def voice_agent(
    pipeline: PipelineConfig, heard: list[str], *, world_base_url: str
) -> AsyncIterator[tuple[str, VoiceAgentServer]]:
    """The rules-brain voice agent hearing ``heard`` in order; yields its URL and server."""
    asr = ScriptedASR(heard, then="Sorry, please go on.")
    stack = SpeechStack(asr=asr, tts=SyntheticTTS(), vad=EnergyVad, voice="agent")
    agent = AcmeAgent(load_agent_files(), AgentSettings(brain="rules"))
    async with httpx2.AsyncClient(timeout=10.0) as client:
        server = VoiceAgentServer(
            agent, WORLD, pipeline, stack, client, world_base_url=world_base_url
        )
        async with running_server(server) as running:
            yield f"ws://127.0.0.1:{bound_port(running)}/", server


async def run_voice_trial(
    sandbox: Sandbox, pipeline: PipelineConfig, case: Scenario, persona: Persona
) -> VoiceOutcome:
    """One graded call of the scripted ``case`` against the agent running ``pipeline``."""
    trial_id = TrialId(new_id())
    brief = TrialBrief(
        trial_id=trial_id, scenario=case, persona=persona, channel=Channel.VOICE, seed=SEED
    )
    sink = RecordingSink(trial_id)
    world = await sandbox.open(brief, WORLD, sink)
    if world.http_url is None:
        raise RuntimeError("the sandbox gave the trial no HTTP tools")
    base_url = world.http_url.removesuffix(f"/t/{trial_id}")
    before = world.snapshot()
    async with voice_agent(pipeline, list(case.user_turns or []), world_base_url=base_url) as (
        url,
        _,
    ):
        target = WebSocketVoiceTarget(WebSocketVoiceAdapterConfig(type="websocket", url=url))
        ctx = SessionContext(
            trial_id=trial_id,
            seed=SEED,
            world_http_url=world.http_url,
            world_mcp_url=world.mcp_url,
            timeout_s=15.0,
        )
        result = await VoiceDriver(target, asr=FakeASR()).run(
            ctx, brief, ScriptedSimulator(brief, sink.clock), sink
        )
    after = world.snapshot()
    await world.close()
    trial = Trial(
        id=trial_id,
        run_id=RunId(new_id()),
        scenario_id=case.id,
        persona_id=persona.id,
        channel=Channel.VOICE,
        index=0,
        seed=SEED,
        status=result.status,
        end_reason=result.end_reason,
        started_at=datetime.now(UTC),
        duration_ms=sink.now_ms(),
        cost_usd=Decimal(0),
        world_before=before,
        world_after=after,
        turns=result.turns,
    )
    context = GradeContext(trial=trial, events=sink.events, scenario=case, persona=persona)
    return VoiceOutcome(trial, sink.events, await grade_trial(context, build_graders(case)))
