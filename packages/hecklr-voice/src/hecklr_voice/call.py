"""One voice call: the frame sender, the agent listener and the turn-taking conversation loop.

Three tasks share one state object. The sender paces 20 ms frames on the send clock and records
them; the receiver stamps, records and segments the agent's frames; the conversation loop asks
the simulator for each message, waits for its turn, speaks, and listens to the reply, barging
in or backchanneling as the persona's policy says. Events follow ADR 0006 and ADR 0004a; their
timing rules are in ADR 0009a.
"""

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, NoReturn

import anyio
import anyio.to_thread

from hecklr_adapters import AgentError, AgentTimeoutError
from hecklr_schemas import (
    AudioLink,
    AudioSpanPayload,
    CustomerSimulator,
    DialogueTurn,
    EndReason,
    ErrorPayload,
    EventId,
    EventSink,
    InterruptPayload,
    SessionContext,
    SilencePayload,
    SimEnd,
    TrialBrief,
    UtterancePayload,
    VoiceProfile,
)
from hecklr_schemas.events import Party, SilenceReason
from hecklr_voice.asr import ASR
from hecklr_voice.audio import (
    FRAME_MS,
    FRAME_SAMPLES,
    SAMPLE_RATE,
    SAMPLES_PER_MS,
    bytes_to_pcm16,
    stream_rng,
    stream_seed,
)
from hecklr_voice.impair import (
    ImpairmentChain,
    RenderedClip,
    Segment,
    SegmentContent,
    render_clip,
    render_idle,
)
from hecklr_voice.network import SendJitter
from hecklr_voice.pool import run_dsp
from hecklr_voice.recording import CallRecorder, RecordingPaths
from hecklr_voice.streams import AgentSpeech, CallerStream, Clip
from hecklr_voice.streams import Segment as SpeechSegment
from hecklr_voice.timing import FramePacer, MediaClock, TrialTime
from hecklr_voice.tts import TTS, SpeechRequest
from hecklr_voice.turntaking import ReplyPlan, TurnTakingPolicy, UtterancePlan
from hecklr_voice.vad import Vad

DriverStatus = Literal["ok", "agent_error", "timeout"]

_FILLER_GAP_MS: Final = 120
"""Pause between a filler ("uh,") and the message it starts."""
_SPEAKING_TOLERANCE_MS: Final = 2 * FRAME_MS
"""How recent agent speech must be for the agent to count as talking right now."""


@dataclass(frozen=True, slots=True)
class VoiceSettings:
    """Harness-side voice parameters (not persona behavior)."""

    hangover_ms: int = 250
    """Quiet that ends an agent speech segment; shorter pauses stay inside one segment."""
    greeting_wait_ms: int = 1_500
    """How long the caller waits, after connecting, for the agent to speak first."""
    backchannel_window_ms: int = 1_000
    """An agent that stops within this long after a backchannel was stopped by it."""
    idle_seconds: float = 4.0
    """Length of the looped idle-line render (background noise between utterances)."""
    close_timeout_s: float = 5.0
    """Bound on hanging up and on other shutdown steps."""


@dataclass(frozen=True, slots=True)
class CallStats:
    """Transport health of one call, for tests and the SC3 benchmark."""

    frames_sent: int
    frames_received: int
    underruns: int
    """Frames sent a full frame period or more after they were due."""
    max_lateness_ms: float
    lost_frames: int
    """Frames dropped by the simulated network (packet loss), not by the harness."""


@dataclass(frozen=True, slots=True)
class Outcome:
    """How the call ended."""

    status: DriverStatus
    end_reason: EndReason
    error: str | None = None


class CallEnded(Exception):  # noqa: N818 -- control flow, not an error condition
    """Unwinds the conversation loop when the call must end now."""

    def __init__(self, outcome: Outcome) -> None:
        super().__init__(outcome.error or outcome.end_reason.value)
        self.outcome = outcome


@dataclass(slots=True)
class _UserTurn:
    index: int
    text: str
    start_ms: float
    end_ms: float
    answered: bool = False


@dataclass(slots=True)
class _AgentTurn:
    index: int
    onset_ms: float
    latency_ms: int | None
    plan: ReplyPlan | None
    segments: list[SpeechSegment] = field(default_factory=list)
    span_ids: list[EventId] = field(default_factory=list)
    accepting: bool = True
    acted: bool = False
    """The planned barge-in or backchannel has been tried; it is tried once per reply."""
    interrupted: bool = False
    emitted: bool = False


@dataclass(slots=True)
class _Interrupt:
    t_ms: float
    style: Literal["barge_in", "backchannel"]
    segment: SpeechSegment
    resolved: bool = False


@dataclass(frozen=True, slots=True)
class CallParts:
    """Everything a call needs besides the link (built per trial by the driver)."""

    ctx: SessionContext
    brief: TrialBrief
    voice: VoiceProfile
    simulator: CustomerSimulator
    sink: EventSink
    tts: TTS
    asr: ASR
    vad: Vad
    settings: VoiceSettings
    recordings: tuple[Path, RecordingPaths] | None
    """Run directory and this call's recording paths, or ``None`` not to write WAVs."""


class Call:
    """State and tasks of one call; create one per trial attempt."""

    def __init__(self, parts: CallParts, clock: TrialTime, deadline_ms: float) -> None:
        self._p = parts
        self._clock = clock
        self._deadline_ms = deadline_ms
        self._chain = ImpairmentChain.for_voice(parts.voice)
        self._policy = TurnTakingPolicy(parts.brief.persona.behaviors, parts.brief.seed)
        self._jitter = SendJitter(parts.voice.jitter_ms, stream_rng(parts.brief.seed, "jitter"))
        self._recorder = CallRecorder()
        self._media = MediaClock()
        self._agent = AgentSpeech(parts.settings.hangover_ms, self._segment_opened, self._closed)
        self._changed = anyio.Event()
        self._failure: Outcome | None = None
        self._turns = 0
        self._user_ordinal = 0
        self._reply_ordinal = 0
        self._last_user: _UserTurn | None = None
        self._agent_turn: _AgentTurn | None = None
        self._agent_turns: list[_AgentTurn] = []
        self._dialogue: dict[int, DialogueTurn] = {}
        self._interrupts: list[_Interrupt] = []
        self._prepared: dict[str, RenderedClip] = {}
        self._preparing: dict[str, anyio.Event] = {}
        self._frames_received = 0
        self._lost_frames = 0
        self._agent_origin_sample: int | None = None
        self._stream: CallerStream | None = None
        self._pacer: FramePacer | None = None

    @property
    def turns(self) -> int:
        """Turns started by both parties."""
        return self._turns

    # --- lifecycle ---------------------------------------------------------------------------

    async def run(self, link: AudioLink) -> Outcome:
        """Converse over ``link`` until the call ends; harness failures propagate."""
        idle = await run_dsp(
            render_idle,
            self._chain,
            stream_seed(self._p.brief.seed, "idle"),
            self._p.settings.idle_seconds,
        )
        self._stream = CallerStream(bytes_to_pcm16(idle))
        try:
            async with anyio.create_task_group() as group:
                self._pacer = FramePacer(
                    self._clock.now_ms(), self._clock.now_ms, self._clock.sleep_until
                )
                group.start_soon(self._send_loop, link)
                group.start_soon(self._receive_loop, link)
                group.start_soon(self._watchdog)
                outcome = await self._conversation()
                group.cancel_scope.cancel()
        except BaseExceptionGroup as group_error:
            # One task failed for a harness reason: surface that exception itself, so the
            # orchestrator's retry policy sees its real type.
            if len(group_error.exceptions) == 1:
                raise group_error.exceptions[0] from None
            raise
        return outcome

    async def finish(self) -> None:
        """Close what is still open, log the rest, and write the recordings."""
        self._agent.close()
        for pending in self._interrupts:
            self._resolve(pending, None)
        await self._finalize_agent_turns()
        pacer = self._pacer
        if pacer is not None and pacer.underruns:
            self._p.sink.emit(
                "system",
                ErrorPayload(
                    source="harness",
                    message=(
                        f"{pacer.underruns} caller frame(s) left a frame period or more late "
                        f"(worst {pacer.max_lateness_ms:.0f} ms); voice timings may be skewed"
                    ),
                ),
            )
        if self._p.recordings is not None:
            run_dir, paths = self._p.recordings
            await anyio.to_thread.run_sync(self._recorder.write, run_dir, paths)

    def _line(self) -> tuple[CallerStream, FramePacer]:
        """The send side, which exists once ``run`` has started the call."""
        if self._stream is None or self._pacer is None:
            raise RuntimeError("the call has not started")
        return self._stream, self._pacer

    def stats(self) -> CallStats:
        """Transport health so far."""
        pacer = self._pacer
        return CallStats(
            frames_sent=self._stream.next_frame if self._stream else 0,
            frames_received=self._frames_received,
            underruns=pacer.underruns if pacer else 0,
            max_lateness_ms=pacer.max_lateness_ms if pacer else 0.0,
            lost_frames=self._lost_frames,
        )

    def _end(self, outcome: Outcome) -> None:
        """Record the first reason the call must end and wake the conversation loop."""
        if self._failure is None:
            self._failure = outcome
            if outcome.error is not None:
                self._log_error(outcome.error)
        self._notify()

    # --- tasks -------------------------------------------------------------------------------

    async def _send_loop(self, link: AudioLink) -> None:
        stream, pacer = self._line()
        origin_sample = round(pacer.origin_ms * SAMPLES_PER_MS)
        index = 0
        while True:
            await pacer.wait(index, self._jitter.next_delay_ms())
            pcm = stream.take(index)
            self._recorder.caller.write(origin_sample + index * FRAME_SAMPLES, pcm)
            try:
                await link.send_frame(pcm.astype("<i2").tobytes(), SAMPLE_RATE)
            except AgentError as exc:
                self._end(agent_failed(exc))
                return
            index += 1
            self._notify()

    async def _receive_loop(self, link: AudioLink) -> None:
        try:
            async for frame in link.frames():
                await self._on_frame(frame.pcm, frame.sample_rate, frame.sample_index)
        except AgentError as exc:
            self._end(agent_failed(exc))
            return
        self._agent.close()
        self._end(Outcome("ok", EndReason.AGENT_ENDED))

    async def _watchdog(self) -> None:
        """Close the agent's segment when its stream goes quiet by sending nothing at all."""
        while True:
            await anyio.sleep(self._p.settings.hangover_ms / 4000)
            self._agent.quiet_until(self._clock.now_ms() - FRAME_MS)
            self._notify()

    async def _on_frame(self, pcm_bytes: bytes, sample_rate: int, sample_index: int) -> None:
        """Stamp, record and segment one agent frame (arrival is stamped before any await)."""
        if sample_rate != SAMPLE_RATE:
            raise ValueError(
                f"voice adapters must deliver {SAMPLE_RATE} Hz frames, got {sample_rate} Hz"
            )
        arrival = self._clock.now_ms()
        pcm = bytes_to_pcm16(pcm_bytes)
        self._media.observe(sample_index, arrival)
        if self._agent_origin_sample is None:
            self._agent_origin_sample = round(arrival * SAMPLES_PER_MS) - sample_index
        position = self._agent_origin_sample + sample_index
        self._recorder.agent.write(position, pcm)
        self._frames_received += 1
        start_ms = self._media.to_ms(sample_index)
        self._agent.frame(start_ms, position, pcm, await self._p.vad.is_speech(pcm))
        self._notify()

    # --- conversation ------------------------------------------------------------------------

    async def _conversation(self) -> Outcome:
        try:
            await self._greeting()
            while True:
                self._check_limits()
                await self._finalize_agent_turns()
                move = await self._p.simulator.next_turn(self._dialogue_so_far())
                if isinstance(move, SimEnd):
                    if move.closing_text and self._fits():
                        await self._caller_turn(move.closing_text)
                    return Outcome("ok", move.reason)
                await self._caller_turn(move.text)
        except CallEnded as ended:
            return ended.outcome

    def _dialogue_so_far(self) -> tuple[DialogueTurn, ...]:
        return tuple(self._dialogue[index] for index in sorted(self._dialogue))

    def _fits(self) -> bool:
        within_turns = self._turns + 2 <= self._p.brief.scenario.limits.max_turns
        return within_turns and self._clock.now_ms() < self._deadline_ms

    def _time_out(self, message: str, reason: EndReason) -> NoReturn:
        """End the call as a ``timeout``, logged like every agent failure."""
        self._log_error(message)
        raise CallEnded(Outcome("timeout", reason, message))

    def _check_limits(self) -> None:
        if self._clock.now_ms() >= self._deadline_ms:
            raise CallEnded(Outcome("ok", EndReason.MAX_DURATION))
        if self._turns + 2 > self._p.brief.scenario.limits.max_turns:
            raise CallEnded(Outcome("ok", EndReason.MAX_TURNS))

    async def _greeting(self) -> None:
        wait_until = self._clock.now_ms() + self._p.settings.greeting_wait_ms
        if await self._until(lambda: self._agent.open is not None, wait_until):
            await self._hear_out()

    async def _caller_turn(self, text: str) -> None:
        ordinal = self._user_ordinal
        self._user_ordinal += 1
        plan = self._policy.plan_utterance(ordinal, text)
        clip = await self._render_utterance(plan, ordinal)
        reason = await self._wait_for_turn(ordinal)
        self._check_limits()
        user = await self._speak(plan.text, clip, gap_reason=reason, plan=plan)
        await self._await_reply(user)

    async def _wait_for_turn(self, ordinal: int) -> SilenceReason:
        """Wait the persona's gap after the agent stops (or a re-engagement silence)."""
        gap = self._policy.response_gap_ms(ordinal)
        silence = self._policy.silence_ms(ordinal)
        while True:
            last = self._last_quiet_ms()
            if last is None:
                return "turn_gap"
            pause = silence if silence is not None else gap
            if not await self._until(lambda: self._agent.open is not None, last + pause):
                return "re_engagement" if silence is not None else "turn_gap"
            if silence is not None:
                # The agent spoke into the silence (a re-engagement prompt): answer it normally.
                onset = self._agent.open.start_ms if self._agent.open else self._clock.now_ms()
                self._emit_silence("user", last, onset, "re_engagement")
                silence = None
            await self._listen()

    def _last_quiet_ms(self) -> float | None:
        """When the line last went quiet: the later of the agent's and the caller's speech end."""
        ends = [self._agent.last_end_ms, self._last_user.end_ms if self._last_user else None]
        known = [end for end in ends if end is not None]
        return max(known) if known else None

    async def _await_reply(self, user: _UserTurn) -> None:
        """Wait for the agent to answer ``user`` and hear the reply out."""
        await self._wait_for_answer(user)
        await self._hear_out()

    async def _hear_out(self) -> None:
        """Listen until the agent is quiet; a barge-in is a caller turn that wants its own reply."""
        while (interjection := await self._listen()) is not None:
            text, clip = interjection
            await self._wait_for_answer(await self._speak(text, clip, gap_reason=None, plan=None))

    async def _wait_for_answer(self, user: _UserTurn) -> None:
        timeout_ms = user.end_ms + self._p.ctx.timeout_s * 1000
        answered = await self._until(
            lambda: self._agent_spoke_after(user.end_ms), min(timeout_ms, self._deadline_ms)
        )
        if answered:
            return
        self._emit_silence("agent", user.end_ms, self._clock.now_ms(), "turn_gap")
        if timeout_ms > self._deadline_ms:
            limit = self._p.brief.scenario.limits.max_duration_s
            message = f"scenario max_duration_s={limit} ran out waiting for the agent"
            self._time_out(message, EndReason.MAX_DURATION)
        self._time_out(f"agent did not answer within {self._p.ctx.timeout_s} s", EndReason.ERROR)

    def _agent_spoke_after(self, t_ms: float) -> bool:
        if self._agent.open is not None and self._agent.open.last_ms > t_ms:
            return True
        return self._agent.last_end_ms is not None and self._agent.last_end_ms > t_ms

    async def _listen(self) -> tuple[str, RenderedClip] | None:
        """Listen until the agent is quiet; return an interjection if the caller barges in."""
        turn = self._agent_turn
        plan = turn.plan if turn is not None else None
        while self._agent.open is not None:
            if self._clock.now_ms() >= self._deadline_ms:
                limit = self._p.brief.scenario.limits.max_duration_s
                message = f"scenario max_duration_s={limit} ran out mid-reply"
                self._time_out(message, EndReason.MAX_DURATION)
            action_ms = _action_time(turn, plan)
            if turn is not None and action_ms is not None and self._clock.now_ms() >= action_ms:
                turn.acted = True
                interjection = await self._act(turn, plan)
                if interjection is not None:
                    return interjection
                continue
            wake = action_ms if action_ms is not None else self._deadline_ms
            await self._until(lambda: self._agent.open is None, wake)
        return None

    async def _act(
        self, turn: _AgentTurn | None, plan: ReplyPlan | None
    ) -> tuple[str, RenderedClip] | None:
        if turn is None or plan is None or not self._speaking_now():
            return None
        if plan.barge_in_after_ms is not None:
            if not self._fits():
                return None
            text = self._policy.interjection(turn.index)
            clip = await self._prepare(
                text, stream_seed(self._p.brief.seed, "interjection", turn.index)
            )
            return (text, clip) if self._speaking_now() else None
        text = self._policy.backchannel(turn.index)
        clip = await self._prepare(text, stream_seed(self._p.brief.seed, "backchannel", turn.index))
        if self._speaking_now():
            self._backchannel(clip)
        return None

    def _speaking_now(self) -> bool:
        return self._agent.speaking_at(self._clock.now_ms(), _SPEAKING_TOLERANCE_MS)

    # --- speaking ----------------------------------------------------------------------------

    async def _render_utterance(self, plan: UtterancePlan, ordinal: int) -> RenderedClip:
        texts = ([plan.filler] if plan.filler else []) + list(plan.parts)
        audios = [await self._p.tts.synthesize(self._request(text)) for text in texts]
        segments: list[Segment] = []
        if plan.filler:
            segments += [Segment("filler", audios.pop(0)), Segment.pause(_FILLER_GAP_MS)]
        segments.append(Segment("speech", audios[0]))
        if plan.pause_ms is not None:
            segments += [Segment.pause(plan.pause_ms), Segment("speech", audios[1])]
        seed = stream_seed(self._p.brief.seed, "utterance-clip", ordinal)
        return await run_dsp(render_clip, tuple(segments), self._chain, seed)

    async def _prepare(self, text: str, seed: int) -> RenderedClip:
        """A short interjection or backchannel clip, rendered once per call and reused."""
        if text in self._prepared:
            return self._prepared[text]
        if text in self._preparing:
            await self._preparing[text].wait()
            return self._prepared[text]
        self._preparing[text] = anyio.Event()
        audio = await self._p.tts.synthesize(self._request(text))
        content: SegmentContent = "backchannel" if len(text.split()) == 1 else "speech"
        self._prepared[text] = await run_dsp(
            render_clip, (Segment(content, audio),), self._chain, seed
        )
        self._preparing[text].set()
        return self._prepared[text]

    def _request(self, text: str) -> SpeechRequest:
        tts = self._p.voice.tts
        return SpeechRequest(
            text=text,
            voice=tts.voice,
            rate=tts.rate,
            pitch_semitones=tts.pitch_semitones,
            language=self._p.brief.persona.language,
        )

    def _play(self, clip: RenderedClip) -> tuple[Clip, float]:
        """Queue a clip; returns it and the trial time its first sample is sent."""
        stream, pacer = self._line()
        queued = stream.play(bytes_to_pcm16(clip.pcm))
        self._lost_frames += clip.lost_frames
        return queued, pacer.due_ms(queued.start_frame)

    async def _speak(
        self,
        text: str,
        clip: RenderedClip,
        *,
        gap_reason: SilenceReason | None,
        plan: UtterancePlan | None,
    ) -> _UserTurn:
        """Say one caller turn and log it once said."""
        if not clip.spans:
            raise ValueError(f"the caller's clip for {text!r} has no audible speech")
        barging = self._speaking_now()
        if self._agent_turn is not None:
            self._agent_turn.accepting = False
        index = self._next_turn()
        queued, start_ms = self._play(clip)
        spans = [
            (span, start_ms + span.start / SAMPLES_PER_MS, start_ms + span.end / SAMPLES_PER_MS)
            for span in clip.spans
        ]
        user = _UserTurn(index, text, spans[0][1], spans[-1][2])
        quiet = self._last_quiet_ms()
        self._last_user = user
        self._dialogue[index] = DialogueTurn(speaker="user", text=text)
        if gap_reason is not None and quiet is not None:
            self._emit_silence("user", quiet, user.start_ms, gap_reason)
        if barging and self._agent.open is not None and self._agent_turn is not None:
            self._agent_turn.interrupted = True
            self._interrupts.append(_Interrupt(user.start_ms, "barge_in", self._agent.open))
        await self._until(queued.done.is_set, math.inf)
        span_ids = [
            self._emit_span("user", span.content, begin, end, queued, span.start)
            for span, begin, end in spans
        ]
        if plan is not None and plan.pause_ms is not None and len(spans) >= 2:
            self._emit_silence("user", spans[-2][2], spans[-1][1], "hesitation")
        speech_ids = [
            span_id
            for (span, _, _), span_id in zip(spans, span_ids, strict=True)
            if span.content == "speech"
        ]
        payload = UtterancePayload(
            text=text,
            turn=index,
            end_ms=_ms(user.end_ms),
            audio_span_id=speech_ids[0] if speech_ids else span_ids[0],
        )
        self._p.sink.emit("user", payload, t_ms=_ms(user.start_ms))
        return user

    def _backchannel(self, clip: RenderedClip) -> None:
        if self._agent.open is None:
            return
        queued, start_ms = self._play(clip)
        span = clip.spans[0]
        begin = start_ms + span.start / SAMPLES_PER_MS
        self._interrupts.append(_Interrupt(begin, "backchannel", self._agent.open))
        self._emit_span(
            "user", "backchannel", begin, start_ms + span.end / SAMPLES_PER_MS, queued, span.start
        )

    # --- the agent's side --------------------------------------------------------------------

    def _segment_opened(self, segment: SpeechSegment) -> None:
        turn = self._agent_turn
        if turn is None or not turn.accepting:
            turn = self._open_agent_turn(segment.start_ms)
        turn.segments.append(segment)

    def _open_agent_turn(self, onset_ms: float) -> _AgentTurn:
        answers = self._last_user if self._last_user and not self._last_user.answered else None
        latency = None
        if answers is not None:
            answers.answered = True
            if onset_ms >= answers.end_ms:
                latency = _ms(onset_ms - answers.end_ms)
                self._emit_silence("agent", answers.end_ms, onset_ms, "turn_gap")
        planned = answers is not None or not self._agent_turns
        plan = self._policy.plan_reply(self._reply_ordinal) if planned else None
        self._reply_ordinal += int(planned)
        turn = _AgentTurn(self._next_turn(), onset_ms, latency, plan)
        self._agent_turn = turn
        self._agent_turns.append(turn)
        return turn

    def _closed(self, segment: SpeechSegment) -> None:
        turn = next(turn for turn in self._agent_turns if segment in turn.segments)
        turn.span_ids.append(
            self._p.sink.emit(
                "agent",
                AudioSpanPayload(
                    start_ms=_ms(segment.start_ms),
                    end_ms=_ms(segment.last_ms),
                    start_sample=segment.start_sample,
                    sample_rate=SAMPLE_RATE,
                    recording=self._recording("agent"),
                ),
                t_ms=_ms(segment.start_ms),
            ).id
        )
        for pending in self._interrupts:
            if pending.segment is segment:
                self._resolve(pending, segment.last_ms)

    def _resolve(self, pending: _Interrupt, stopped_ms: float | None) -> None:
        if pending.resolved:
            return
        pending.resolved = True
        reaction = None if stopped_ms is None else max(0.0, stopped_ms - pending.t_ms)
        if pending.style == "backchannel" and reaction is not None:
            reaction = reaction if reaction <= self._p.settings.backchannel_window_ms else None
        payload = InterruptPayload(
            target="agent",
            style=pending.style,
            reaction_ms=None if reaction is None else _ms(reaction),
        )
        self._p.sink.emit("user", payload, t_ms=_ms(pending.t_ms))

    async def _finalize_agent_turns(self) -> None:
        """Transcribe and log every agent turn whose speech has ended."""
        for turn in self._agent_turns:
            if turn.emitted or not turn.segments or not all(s.closed for s in turn.segments):
                continue
            turn.accepting = False
            turn.emitted = True
            first, last = turn.segments[0], turn.segments[-1]
            audio = self._recorder.agent.read(first.start_sample, last.last_sample)
            heard = await self._p.asr.transcribe(audio, language=self._p.brief.persona.language)
            payload = UtterancePayload(
                text=heard.text,
                turn=turn.index,
                end_ms=_ms(last.last_ms),
                latency_ms=turn.latency_ms,
                interrupted=turn.interrupted,
                asr_confidence=heard.confidence,
                audio_span_id=turn.span_ids[0],
            )
            self._p.sink.emit("agent", payload, t_ms=_ms(first.start_ms))
            self._dialogue[turn.index] = DialogueTurn(speaker="agent", text=heard.text)

    # --- plumbing ----------------------------------------------------------------------------

    def _next_turn(self) -> int:
        index = self._turns
        self._turns += 1
        return index

    def _notify(self) -> None:
        self._changed.set()
        self._changed = anyio.Event()

    async def _until(self, condition: Callable[[], bool], deadline_ms: float) -> bool:
        """Wait for ``condition`` until ``deadline_ms``; ends the call on a failure."""
        while True:
            if self._failure is not None:
                raise CallEnded(self._failure)
            if condition():
                return True
            remaining_ms = deadline_ms - self._clock.now_ms()
            if remaining_ms <= 0:
                return False
            changed = self._changed
            with anyio.move_on_after(remaining_ms / 1000):
                await changed.wait()

    def _recording(self, side: Party) -> str | None:
        if self._p.recordings is None:
            return None
        paths = self._p.recordings[1]
        return paths.caller if side == "user" else paths.agent

    def _emit_span(
        self,
        actor: Party,
        content: Literal["speech", "filler", "backchannel"],
        begin: float,
        end: float,
        queued: Clip,
        offset: int,
    ) -> EventId:
        _, pacer = self._line()
        start_sample = (
            round(pacer.origin_ms * SAMPLES_PER_MS) + queued.start_frame * FRAME_SAMPLES + offset
        )
        payload = AudioSpanPayload(
            start_ms=_ms(begin),
            end_ms=_ms(end),
            content=content,
            start_sample=start_sample,
            sample_rate=SAMPLE_RATE,
            recording=self._recording(actor),
        )
        return self._p.sink.emit(actor, payload, t_ms=_ms(begin)).id

    def _emit_silence(
        self, party: Party, start_ms: float, end_ms: float, reason: SilenceReason
    ) -> None:
        begin, end = _ms(start_ms), _ms(end_ms)
        if end <= begin:
            return
        payload = SilencePayload(start_ms=begin, end_ms=end, attributed_to=party, reason=reason)
        self._p.sink.emit("system", payload, t_ms=begin)

    def _log_error(self, message: str) -> None:
        self._p.sink.emit("system", ErrorPayload(source="agent", message=message))


def _action_time(turn: _AgentTurn | None, plan: ReplyPlan | None) -> float | None:
    if turn is None or plan is None or turn.acted:
        return None
    delay = (
        plan.barge_in_after_ms if plan.barge_in_after_ms is not None else plan.backchannel_after_ms
    )
    return None if delay is None else turn.onset_ms + delay


def agent_failed(exc: AgentError) -> Outcome:
    """Map an adapter failure to the outcome ADR 0006a prescribes."""
    if isinstance(exc, AgentTimeoutError):
        return Outcome("timeout", EndReason.ERROR, str(exc))
    return Outcome("agent_error", EndReason.ERROR, str(exc))


def _ms(value: float) -> int:
    """A trial-clock instant or duration as the integer milliseconds events carry."""
    return max(0, round(value))
