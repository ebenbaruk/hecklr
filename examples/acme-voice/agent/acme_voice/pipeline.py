"""One phone call with the acme agent: VAD -> ASR -> acme brain -> TTS, with barge-in.

The call hears the caller as 16 kHz audio and cuts it into 20 ms frames. A frame is speech when
the VAD says so. The caller's turn starts at the first speech frame (with ``PREROLL_MS`` of audio
before it, so the ASR hears the first syllable whole) and ends after ``end_of_speech_ms`` of
silence. Each finished turn is transcribed, normalized, answered by the acme brain and spoken,
one turn at a time, after ``tts_pre_delay_ms``. Transcription starts speculatively once the
caller has been quiet for ``SPECULATE_AFTER_MS``: a batch recognizer takes a few hundred
milliseconds, and starting it early takes it off the response path whenever the caller does
not resume (the result is dropped when they do). A call runs one early transcription at a time:
a caller who pauses often would otherwise queue a transcription per pause, and the backlog
delayed answers by seconds.

The caller *takes the floor* once they have spoken for ``barge_in_min_speech_ms`` without a
break. With ``barge_in`` on, taking the floor stops the agent mid-reply (a ``clear`` on the
transport drops what the caller has not heard yet) and drops any reply still being prepared for
an earlier turn: the caller has moved on. With it off, the agent finishes what it is saying and
answers every turn in order, talking over the caller.

A reply that hands the caller to a human (the emergency ticket) ends the call once it has been
said.
"""

from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Protocol

import anyio
import anyio.abc
import numpy as np
import structlog
from acme_agent import Reply

from acme_voice.config import PipelineConfig
from acme_voice.normalize import normalize_transcript
from hecklr_voice import ASR, TTS, SpeechRequest, Vad, VadFactory
from hecklr_voice.audio import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE, Pcm16, to_pcm16

_log = structlog.get_logger(__name__)

GREETING: Final = "Thanks for calling Acme Maintenance. How can I help you today?"
PREROLL_MS: Final = 200
"""Audio kept from before the first speech frame of a turn."""
SPECULATE_AFTER_MS: Final = 100
"""Caller silence after which the turn so far is transcribed, before the turn has ended."""
TURN_QUEUE: Final = 16
"""Finished caller turns waiting to be answered; more means the agent fell hopelessly behind."""

Respond = Callable[[str], Awaitable[Reply]]
"""Answers one normalized caller message (the acme brain, with this call's session and tools)."""
Clock = Callable[[], float]


class Mouth(Protocol):
    """How a call's audio reaches the caller."""

    async def play(self, pcm: Pcm16) -> None:
        """Send speech; the transport plays it in real time after anything still playing."""
        ...

    async def clear(self) -> None:
        """Drop whatever the caller has not heard yet."""
        ...

    async def hang_up(self) -> None:
        """End the call from the agent's side."""
        ...


@dataclass(frozen=True, slots=True)
class SpeechStack:
    """The models a call hears and speaks with; shared by every call of a server."""

    asr: ASR
    tts: TTS
    vad: VadFactory
    voice: str
    language: str = "en-US"


async def finish[T](work: Awaitable[T]) -> T:
    """Await ``work`` even if the call ends meanwhile.

    The DSP pool runs speech models in worker processes and kills a worker whose job is
    cancelled; the next job then starts a new worker and loads its model again, which put
    4-6 s on another call's answer whenever a call hung up mid-transcription. Letting a hang-up
    wait for the job (a few hundred milliseconds) keeps the loaded workers alive.
    """
    with anyio.CancelScope(shield=True):
        result = await work
    # Returned after the block: a CancelScope may swallow exceptions, so mypy would see a path
    # that falls off the end of the function.
    return result  # noqa: RET504 -- see above


class Transcription:
    """A transcription running in the background, awaited when its turn is answered."""

    def __init__(self) -> None:
        self._done = anyio.Event()
        self._text = ""
        self._error: Exception | None = None

    async def run(self, asr: ASR, pcm: Pcm16, language: str) -> None:
        """Transcribe; a failure is kept and raised to whoever awaits the result."""
        try:
            self._text = (await finish(asr.transcribe(pcm, language=language))).text
        except Exception as exc:  # noqa: BLE001 -- re-raised by result(), never lost
            self._error = exc
        finally:
            self._done.set()

    @property
    def done(self) -> bool:
        """Whether the transcript (or its failure) is ready."""
        return self._done.is_set()

    async def result(self) -> str:
        """The transcript, once ready."""
        await self._done.wait()
        if self._error is not None:
            raise self._error
        return self._text


@dataclass(frozen=True, slots=True)
class CallerTurn:
    """A finished caller turn: its audio, the floor count then, and any early transcript."""

    pcm: Pcm16
    floor: int
    ended_ms: float
    """When the agent decided the turn was over, on its clock."""
    transcription: Transcription | None = None


class VoiceCall:
    """The state and the answering loop of one call.

    Feed caller audio with :meth:`hear` while :meth:`run` answers; ``run`` returns when the
    agent has hung up (a handoff) and is cancelled by the server when the caller hangs up.
    """

    def __init__(
        self,
        config: PipelineConfig,
        stack: SpeechStack,
        respond: Respond,
        mouth: Mouth,
        clock: Clock,
    ) -> None:
        self._config = config
        self._stack = stack
        self._respond = respond
        self._mouth = mouth
        self._clock = clock
        self._vad: Vad = stack.vad()
        self._pending = np.zeros(0, dtype=np.int16)
        self._preroll: deque[Pcm16] = deque(maxlen=PREROLL_MS // FRAME_MS)
        self._utterance: list[Pcm16] | None = None
        self._speech_ms = 0
        self._silence_ms = 0
        self._took_floor = False
        self._speculation: Transcription | None = None
        """The call's latest early transcription."""
        self._speculated = False
        """Whether ``_speculation`` covers all of the current turn's speech."""
        self._group: anyio.abc.TaskGroup | None = None
        self._over = False
        self.floor = 0
        """How many times the caller has taken the floor."""
        self.speaking_until_ms = 0.0
        """When the caller will have heard everything the agent sent so far."""
        self.heard: list[str] = []
        """What the agent understood of each caller turn (normalized transcripts)."""
        send, receive = anyio.create_memory_object_stream[CallerTurn](TURN_QUEUE)
        self._turns_in, self._turns_out = send, receive

    async def prepare(self) -> None:
        """Load the VAD before audio flows."""
        await self._vad.prepare()

    async def hear(self, samples: Pcm16) -> None:
        """Hear a chunk of 16 kHz caller audio of any length; ignored once the call is over."""
        if self._over:
            return
        self._pending = np.concatenate([self._pending, samples])
        while len(self._pending) >= FRAME_SAMPLES:
            frame, self._pending = self._pending[:FRAME_SAMPLES], self._pending[FRAME_SAMPLES:]
            await self._frame(frame)

    async def run(self) -> None:
        """Greet, then answer caller turns until the agent hands the call over."""
        try:
            async with anyio.create_task_group() as group, self._turns_in, self._turns_out:
                self._group = group
                await self._say(GREETING, self.floor)
                async for turn in self._turns_out:
                    if await self._answer(turn):
                        group.cancel_scope.cancel()
                        return
        finally:
            self._over = True

    @property
    def speaking(self) -> bool:
        """Whether the caller is still hearing the agent."""
        return self._clock() < self.speaking_until_ms

    async def _frame(self, frame: Pcm16) -> None:
        if await self._vad.is_speech(frame):
            if self._utterance is None:
                self._utterance = list(self._preroll)
                self._preroll.clear()
            self._utterance.append(frame)
            self._silence_ms = 0
            self._speculated = False  # the caller resumed: the early transcript is incomplete
            self._speech_ms += FRAME_MS
            if not self._took_floor and self._speech_ms >= self._config.barge_in_min_speech_ms:
                self._took_floor = True
                await self._caller_took_floor()
            return
        self._speech_ms = 0
        if self._utterance is None:
            self._preroll.append(frame)
            return
        self._utterance.append(frame)
        self._silence_ms += FRAME_MS
        end_ms = self._config.end_of_speech_ms
        if not self._speculated and self._silence_ms >= min(SPECULATE_AFTER_MS, end_ms):
            self._speculate(np.concatenate(self._utterance))
        if self._silence_ms >= end_ms:
            early = self._speculation if self._speculated else None
            turn = CallerTurn(np.concatenate(self._utterance), self.floor, self._clock(), early)
            self._utterance, self._silence_ms, self._took_floor = None, 0, False
            self._speculation, self._speculated = None, False
            self._turns_in.send_nowait(turn)

    def _speculate(self, pcm: Pcm16) -> None:
        busy = self._speculation is not None and not self._speculation.done
        if self._group is None or busy:
            return  # the turn is transcribed when it is answered, or at a later pause
        self._speculated = True
        self._speculation = Transcription()
        self._group.start_soon(self._speculation.run, self._stack.asr, pcm, self._stack.language)

    async def _caller_took_floor(self) -> None:
        self.floor += 1
        if self._config.barge_in and self.speaking:
            self.speaking_until_ms = self._clock()
            await self._mouth.clear()

    def _superseded(self, floor: int) -> bool:
        """A reply prepared at ``floor`` is stale: barge-in is on and the caller spoke since."""
        return self._config.barge_in and self.floor != floor

    async def _answer(self, turn: CallerTurn) -> bool:
        """Answer one turn; return whether the agent ended the call."""
        if turn.transcription is not None:
            raw = await turn.transcription.result()
        else:
            stack = self._stack
            raw = (await finish(stack.asr.transcribe(turn.pcm, language=stack.language))).text
        heard_ms = self._clock()
        text = normalize_transcript(raw)
        if not text:
            _log.info("call.heard_nothing", audio_ms=len(turn.pcm) * 1000 // SAMPLE_RATE)
            return False
        self.heard.append(text)
        reply = await self._respond(text)
        superseded = self._superseded(turn.floor)
        _log.info("call.turn", heard=raw, understood=text, reply=reply.text, dropped=superseded)
        if superseded:
            return False
        await self._say(reply.text, turn.floor)
        _log.info(
            "call.answered",
            asr_wait_ms=round(heard_ms - turn.ended_ms),
            answer_ms=round(self._clock() - turn.ended_ms),
        )
        if not reply.ended:
            return False
        # The handoff message must be heard in full before the line closes.
        await anyio.sleep(max(0.0, self.speaking_until_ms - self._clock()) / 1000)
        await self._mouth.hang_up()
        return True

    async def _say(self, text: str, floor: int) -> None:
        if not text.strip():
            return
        if self._config.tts_pre_delay_ms:
            await anyio.sleep(self._config.tts_pre_delay_ms / 1000)
        request = SpeechRequest(text=text, voice=self._stack.voice, language=self._stack.language)
        pcm = to_pcm16(await finish(self._stack.tts.synthesize(request)))
        if self._superseded(floor):
            return
        await self._mouth.play(pcm)
        start = max(self._clock(), self.speaking_until_ms)
        self.speaking_until_ms = start + len(pcm) * 1000 / SAMPLE_RATE
