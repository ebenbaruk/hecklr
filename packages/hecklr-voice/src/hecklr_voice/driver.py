"""``VoiceDriver``: the ``ChannelDriver`` for the voice channel.

It connects to the agent through a ``VoiceTarget`` (an adapter from ``hecklr-adapters``),
runs one :class:`~hecklr_voice.call.Call` per trial, and maps how it ended onto the frozen
``DriverResult`` exactly like the chat driver does (ADR 0006a): agent failures and missed
deadlines are reported in the result, harness failures propagate so the orchestrator retries.
"""

from collections.abc import Callable
from pathlib import Path

import anyio

from hecklr_adapters import AgentError
from hecklr_schemas import (
    AudioLink,
    Channel,
    CustomerSimulator,
    DriverResult,
    EndReason,
    ErrorPayload,
    EventSink,
    SessionContext,
    TrialBrief,
    VoiceTarget,
    new_id,
)
from hecklr_voice.asr import ASR
from hecklr_voice.call import Call, CallParts, CallStats, Outcome, VoiceSettings, agent_failed
from hecklr_voice.pool import warm_dsp_pool
from hecklr_voice.providers import make_tts
from hecklr_voice.recording import RecordingPaths
from hecklr_voice.timing import TrialTime
from hecklr_voice.tts import TtsFactory
from hecklr_voice.vad import EnergyVad, VadFactory


class VoiceDriver:
    """Runs voice calls against one ``VoiceTarget``; safe to share across concurrent trials.

    Args:
        target: The agent's voice adapter.
        asr: How the caller hears the agent (``make_asr(config.models.asr)``).
        tts: Builds the TTS for a persona's ``voice.tts.provider``; pass
            ``functools.partial(make_tts, cache_dir=...)`` to cache synthesized speech.
        vad: Builds one VAD per call for segmenting the agent's speech.
        settings: Harness-side voice parameters.
        run_dir: Where recordings go (``<run_dir>/audio/<trial_id>/<attempt>/``); ``None``
            records nothing.
        on_call_end: Receives each call's transport health (frames, underruns).
    """

    def __init__(
        self,
        target: VoiceTarget,
        *,
        asr: ASR,
        tts: TtsFactory = make_tts,
        vad: VadFactory = EnergyVad,
        settings: VoiceSettings | None = None,
        run_dir: Path | None = None,
        on_call_end: Callable[[CallStats], None] | None = None,
    ) -> None:
        self._target = target
        self._asr = asr
        self._tts = tts
        self._vad = vad
        self._settings = settings or VoiceSettings()
        self._run_dir = run_dir
        self._on_call_end = on_call_end

    @property
    def channel(self) -> Channel:
        """Always ``Channel.VOICE``."""
        return Channel.VOICE

    async def run(
        self,
        ctx: SessionContext,
        brief: TrialBrief,
        simulator: CustomerSimulator,
        sink: EventSink,
    ) -> DriverResult:
        """Place the call and converse to its end.

        Raises:
            ValueError: the persona has no voice profile (a configuration error).
            VoiceConfigError: the profile asks for something this machine cannot simulate.
        """
        voice = brief.persona.voice
        if voice is None:
            raise ValueError(f"persona {brief.persona.id!r} has no voice profile")
        start = sink.now_ms()
        clock = TrialTime(start)
        recordings = None
        if self._run_dir is not None:
            paths = RecordingPaths(f"audio/{brief.trial_id}/{new_id()}")
            recordings = (self._run_dir, paths)
        parts = CallParts(
            ctx=ctx,
            brief=brief,
            voice=voice,
            simulator=simulator,
            sink=sink,
            tts=self._tts(voice.tts.provider),
            asr=self._asr,
            vad=self._vad(),
            settings=self._settings,
            recordings=recordings,
        )
        call = Call(parts, clock, start + brief.scenario.limits.max_duration_s * 1000)
        await warm_dsp_pool()
        link = await self._connect(ctx, sink)
        if isinstance(link, Outcome):
            return _result(link, turns=0)
        try:
            outcome = await call.run(link)
        except BaseException:
            await self._hang_up(link, sink)
            raise
        await self._hang_up(link, sink)
        await call.finish()
        if self._on_call_end is not None:
            self._on_call_end(call.stats())
        return _result(outcome, turns=call.turns)

    async def _connect(self, ctx: SessionContext, sink: EventSink) -> AudioLink | Outcome:
        """Place the call, or say how the agent failed to answer it."""
        with anyio.move_on_after(ctx.timeout_s):
            try:
                return await self._target.connect(ctx)
            except AgentError as exc:
                outcome = agent_failed(exc)
                _log(sink, outcome.error or "")
                return outcome
        message = f"agent did not answer the call within {ctx.timeout_s} s"
        _log(sink, message)
        return Outcome("timeout", EndReason.ERROR, message)

    async def _hang_up(self, link: AudioLink, sink: EventSink) -> None:
        """Hang up, shielded and bounded: a stuck agent cannot hang the trial."""
        with anyio.move_on_after(self._settings.close_timeout_s, shield=True) as scope:
            try:
                await link.hangup()
            except AgentError as exc:
                _log(sink, f"hanging up failed: {exc}")
        if scope.cancelled_caught:
            _log(sink, f"agent did not hang up within {self._settings.close_timeout_s} s")


def _log(sink: EventSink, message: str) -> None:
    sink.emit("system", ErrorPayload(source="agent", message=message))


def _result(outcome: Outcome, *, turns: int) -> DriverResult:
    return DriverResult(
        status=outcome.status, end_reason=outcome.end_reason, turns=turns, error=outcome.error
    )
