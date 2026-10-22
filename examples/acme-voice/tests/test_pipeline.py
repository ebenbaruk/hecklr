"""The voice pipeline of one call: endpointing, speculation, barge-in, replies and handoff."""

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import anyio
import pytest
from acme_agent import Reply
from acme_voice.config import PipelineConfig
from acme_voice.pipeline import GREETING, SpeechStack, Transcription, VoiceCall
from voice_testing import (
    OTHER_SPEECH,
    QUIET,
    SPEECH,
    FakeClock,
    RecordingMouth,
    ScriptedASR,
    ToneTTS,
    config,
    frames,
)

from hecklr_schemas import HandoffPayload
from hecklr_voice import EnergyVad, Transcript
from hecklr_voice.audio import Pcm16

pytestmark = pytest.mark.anyio

Respond = Callable[[str], Awaitable[Reply]]


class Brain:
    """Answers every message with ``reply`` (a handoff when ``handoff``) and keeps the messages."""

    def __init__(self, reply: str = "Sure.", *, handoff: bool = False) -> None:
        self.messages: list[str] = []
        self._reply = Reply(
            reply, handoff=HandoffPayload(target="human", reason="test") if handoff else None
        )
        self.release = anyio.Event()
        self.release.set()

    async def __call__(self, message: str) -> Reply:
        self.messages.append(message)
        await self.release.wait()
        return self._reply


class Call:
    """A ``VoiceCall`` on test doubles, with its answering loop running."""

    def __init__(self, voice: VoiceCall, mouth: RecordingMouth, asr: ScriptedASR, tts: ToneTTS):
        self.voice, self.mouth, self.asr, self.tts = voice, mouth, asr, tts

    async def say(self, *runs: tuple[object, int]) -> None:
        await self.voice.hear(frames(*runs))  # type: ignore[arg-type] # frames of int16 arrays
        await settle()


async def settle() -> None:
    """Let the answering loop catch up with what was heard."""
    for _ in range(20):
        await anyio.sleep(0.005)


@asynccontextmanager
async def call(
    pipeline: PipelineConfig,
    *,
    lines: list[str],
    brain: Respond | None = None,
    clock: FakeClock | None = None,
) -> AsyncIterator[Call]:
    clock = clock or FakeClock()
    mouth, asr, tts = RecordingMouth(clock), ScriptedASR(lines), ToneTTS(1.0)
    stack = SpeechStack(asr=asr, tts=tts, vad=EnergyVad, voice="test")
    voice = VoiceCall(pipeline, stack, brain or Brain(), mouth, clock)
    await voice.prepare()
    async with anyio.create_task_group() as group:
        group.start_soon(voice.run)
        await settle()
        try:
            yield Call(voice, mouth, asr, tts)
        finally:
            group.cancel_scope.cancel()


async def test_greets_then_answers_a_turn_once_the_caller_is_quiet_long_enough() -> None:
    brain = Brain("Could I have your customer number?")
    async with call(config(end_of_speech_ms=600), lines=["See 1,042"], brain=brain) as c:
        assert c.tts.said == [GREETING]
        c.mouth.clock.now = 10_000.0  # the greeting has played out
        await c.say((SPEECH, 25), (QUIET, 29))
        assert brain.messages == []  # 580 ms of silence: the caller may go on
        await c.say((QUIET, 1))
        assert brain.messages == ["C-1042"]  # normalized before the brain sees it
        assert c.tts.said[-1] == "Could I have your customer number?"
        assert len(c.mouth.played) == 2
        assert c.voice.heard == ["C-1042"]


async def test_preroll_keeps_the_first_syllable_and_the_turn_is_transcribed_early() -> None:
    async with call(config(end_of_speech_ms=600), lines=["hello"]) as c:
        await c.say((QUIET, 50), (SPEECH, 10), (QUIET, 5))
        # Transcription started after 100 ms of silence, before the turn ended.
        assert c.asr.heard == [(10 + 10 + 5) * 320]  # 200 ms pre-roll + speech + silence
        await c.say((QUIET, 25))
        assert c.asr.heard == [(10 + 10 + 5) * 320]  # reused, not transcribed again


async def test_a_resumed_turn_drops_the_early_transcript() -> None:
    brain = Brain()
    async with call(config(end_of_speech_ms=600), lines=["whole"], brain=brain) as c:
        await c.say((SPEECH, 10), (QUIET, 10), (SPEECH, 10), (QUIET, 30))
        # Transcribed 100 ms into the first pause, then 100 ms into the last one, which ended
        # the turn: that second transcript is the one answered.
        assert c.asr.heard == [(10 + 5) * 320, (10 + 10 + 10 + 5) * 320]
        assert brain.messages == ["whole"]


@pytest.mark.parametrize(("end_of_speech_ms", "turns"), [(600, 1), (150, 2)])
async def test_f9_a_short_endpoint_splits_a_hesitating_caller(
    end_of_speech_ms: int, turns: int
) -> None:
    """A 300 ms hesitation: one turn at 600 ms endpointing, two at 150 ms (fault F9)."""
    brain = Brain()
    lines = ["one", "two", "three"]
    async with call(config(end_of_speech_ms=end_of_speech_ms), lines=lines, brain=brain) as c:
        await c.say((SPEECH, 20), (QUIET, 15), (SPEECH, 20), (QUIET, 31))
        assert len(brain.messages) == turns


async def test_barge_in_clears_the_reply_after_the_minimum_speech() -> None:
    clock = FakeClock()
    async with call(config(barge_in_min_speech_ms=80), lines=[], clock=clock) as c:
        assert c.voice.speaking  # the greeting is playing
        clock.now = 500.0
        await c.say((SPEECH, 3))
        assert c.mouth.cleared == []  # 60 ms could be a cough
        await c.say((SPEECH, 1))
        assert c.mouth.cleared == [500.0]
        assert not c.voice.speaking


async def test_f8_without_barge_in_the_agent_talks_over_the_caller() -> None:
    clock = FakeClock()
    async with call(config(barge_in=False), lines=[], clock=clock) as c:
        clock.now = 500.0
        await c.say((SPEECH, 20))
        assert c.mouth.cleared == []
        assert c.voice.speaking


@pytest.mark.parametrize(("barge_in", "spoken"), [(True, 1), (False, 2)])
async def test_a_reply_overtaken_by_the_caller_is_dropped_only_with_barge_in(
    barge_in: bool, spoken: int
) -> None:
    clock = FakeClock(now=10_000.0)
    brain = Brain()
    brain.release = anyio.Event()  # the brain is slow
    async with call(config(barge_in=barge_in), lines=["a", "b"], brain=brain, clock=clock) as c:
        clock.now = 20_000.0
        await c.say((SPEECH, 10), (QUIET, 30))  # turn 1 is being answered
        await c.say((OTHER_SPEECH, 10))  # the caller speaks again before the answer
        brain.release.set()
        await c.say((QUIET, 30))
        assert brain.messages == ["a", "b"]
        assert len(c.mouth.played) == 1 + spoken  # the greeting, then the replies kept


async def test_empty_transcripts_are_not_answered() -> None:
    brain = Brain()
    async with call(config(), lines=["  "], brain=brain) as c:
        await c.say((SPEECH, 10), (QUIET, 30))
        assert brain.messages == []
        assert len(c.mouth.played) == 1


async def test_f7_the_pre_delay_comes_before_speaking() -> None:
    async with call(config(tts_pre_delay_ms=150), lines=["hi"]) as c:
        with anyio.fail_after(5):
            await c.mouth.wait_played(1)  # the greeting waits too
        c.mouth.clock.now = 10_000.0
        started = anyio.current_time()
        await c.voice.hear(frames((SPEECH, 10), (QUIET, 30)))
        with anyio.fail_after(5):
            await c.mouth.wait_played(2)
        assert anyio.current_time() - started >= 0.15


async def test_a_handoff_hangs_up_once_the_message_has_played() -> None:
    clock = FakeClock(now=10_000.0)
    brain = Brain("Transferring you to the on-call engineer.", handoff=True)
    async with call(config(), lines=["I smell gas"], brain=brain, clock=clock) as c:
        clock.now = 20_000.0
        await c.voice.hear(frames((SPEECH, 10), (QUIET, 30)))
        await settle()
        assert c.mouth.hung_up == []  # the message is still playing (1 s of tone)
        clock.now = 21_100.0
        await anyio.sleep(1.1)
        assert c.mouth.hung_up == [21_100.0]


async def test_a_failed_early_transcription_is_raised_when_the_turn_is_answered() -> None:
    class BrokenASR:
        @property
        def name(self) -> str:
            return "broken/test"

        async def transcribe(self, pcm: Pcm16, *, language: str) -> Transcript:
            raise RuntimeError("asr down")

    pending = Transcription()
    await pending.run(BrokenASR(), QUIET, "en-US")
    with pytest.raises(RuntimeError, match="asr down"):
        await pending.result()
