"""Real cloud speech: AssemblyAI streaming ASR and OpenAI TTS, next to the local models.

Marked ``live`` and paid: skipped unless the provider's key and ``HECKLR_TEST_BUDGET_USD`` are
set. The reference speech comes from local Kokoro (extra ``local``); AssemblyAI streaming,
AssemblyAI pre-recorded and faster-whisper score the same audio, so each run prints every WER
(with a bootstrap interval) and each cloud ASR's time to hear a turn. The whole file costs about
one cent: a few minutes of AssemblyAI audio and sessions (USD 0.15 per hour) and about 15 seconds
of ``gpt-4o-mini-tts`` speech. On a free AssemblyAI account (5 new streaming sessions per minute)
streaming transcriptions are spaced 13 s apart, so the file takes about five minutes.
"""

import importlib.util
import os
import time
from collections.abc import Sequence
from decimal import Decimal
from typing import Final

import anyio
import numpy as np
import pytest

from hecklr_voice.asr import ASR, AsrSample, Transcript, WhisperASR, measure_asr, reference_samples
from hecklr_voice.asr_assemblyai import AssemblyAIASR, AssemblyAIBatchASR
from hecklr_voice.audio import SAMPLE_RATE, Pcm16
from hecklr_voice.errors import VoiceProviderError
from hecklr_voice.impair import ImpairmentChain
from hecklr_voice.models import DEFAULT_MODELS_DIR
from hecklr_voice.tts import SpeechRequest
from hecklr_voice.tts_cloud import OpenAITTS
from hecklr_voice.tts_kokoro import KokoroTTS

ESTIMATED_COST_USD: Final = Decimal("0.02")
WHISPER: Final = "tiny.en"

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(
        importlib.util.find_spec("kokoro_onnx") is None
        or importlib.util.find_spec("faster_whisper") is None,
        reason="needs the 'local' extra for the reference speech and the local baseline",
    ),
]


def _require(key: str) -> str:
    value = os.environ.get(key)
    if not value:
        pytest.skip(f"{key} is not set")
    if Decimal(os.environ.get("HECKLR_TEST_BUDGET_USD", "0")) < ESTIMATED_COST_USD:
        pytest.skip("HECKLR_TEST_BUDGET_USD does not cover the cloud speech live tests")
    return value


class TimedASR:
    """Records how long each transcription took, against the audio's own length.

    ``spacing_s`` keeps transcriptions that far apart (start to start), untimed, so a free
    AssemblyAI account's 5 streaming sessions per minute are never exceeded.
    """

    def __init__(self, inner: ASR, *, spacing_s: float = 0.0) -> None:
        self.inner = inner
        self.spacing_s = spacing_s
        self.seconds: list[float] = []
        self.audio_seconds: list[float] = []
        self._last_start: float | None = None

    @property
    def name(self) -> str:
        return self.inner.name

    async def transcribe(self, pcm: Pcm16, *, language: str) -> Transcript:
        if self._last_start is not None:
            await anyio.sleep(max(0.0, self._last_start + self.spacing_s - time.perf_counter()))
        start = self._last_start = time.perf_counter()
        heard = await self.inner.transcribe(pcm, language=language)
        self.seconds.append(time.perf_counter() - start)
        self.audio_seconds.append(len(pcm) / SAMPLE_RATE)
        return heard

    def summary(self) -> str:
        seconds, audio = np.array(self.seconds), np.array(self.audio_seconds)
        return (
            f"time to hear a turn: mean {seconds.mean():.2f} s, max {seconds.max():.2f} s "
            f"for turns of mean {audio.mean():.2f} s (real-time factor "
            f"{(seconds / audio).mean():.2f})"
        )


async def _kokoro_samples(chain: ImpairmentChain) -> list[AsrSample]:
    tts = KokoroTTS(models_dir=DEFAULT_MODELS_DIR)
    try:
        return await reference_samples(tts, chain, voice="af_sarah", seed=7)
    except VoiceProviderError as exc:
        pytest.skip(f"Kokoro model unavailable: {exc}")


def _cloud_asrs() -> list[TimedASR]:
    key = _require("ASSEMBLYAI_API_KEY")
    return [TimedASR(AssemblyAIASR(key), spacing_s=13.0), TimedASR(AssemblyAIBatchASR(key))]


async def _compare(label: str, samples: Sequence[AsrSample], clouds: list[TimedASR]) -> list[float]:
    """Each cloud ASR's WER on ``samples``, printed next to faster-whisper's."""
    wers: list[float] = []
    recognizers: list[ASR] = [*clouds, WhisperASR(WHISPER)]
    for asr in recognizers:
        wer = (await measure_asr(asr, samples, seed=1)).wer
        print(
            f"\n[{label}] {asr.name}: WER {wer.value:.1%} "
            f"(95% CI {wer.ci_low:.1%}-{wer.ci_high:.1%}, n={wer.n})"
        )
        if isinstance(asr, TimedASR):
            print(f"[{label}] {asr.name} {asr.summary()}")
            wers.append(wer.value)
    return wers


@pytest.mark.parametrize(
    ("label", "chain"),
    [("clean", ImpairmentChain()), ("cafe 10 dB", ImpairmentChain(environment="cafe", snr_db=10))],
)
async def test_assemblyai_hears_the_reference_set_next_to_faster_whisper(
    label: str, chain: ImpairmentChain
) -> None:
    clouds = _cloud_asrs()
    samples = await _kokoro_samples(chain)

    wers = await _compare(label, samples, clouds)

    assert max(wers) < 0.15


async def test_openai_tts_speech_is_heard_by_both_recognizers() -> None:
    tts = OpenAITTS(_require("OPENAI_API_KEY"), model=OpenAITTS.default_model())
    sentences = (
        "Hi, my boiler stopped working this morning.",
        "Can someone come out on Thursday afternoon?",
        "The engineer was very helpful last time.",
    )
    audio = await tts.synthesize(SpeechRequest(text=sentences[0], voice="coral"))
    assert 1.0 < len(audio) / SAMPLE_RATE < 8.0
    assert float(np.abs(audio).max()) > 0.05
    samples = await reference_samples(
        tts, ImpairmentChain(), voice="coral", seed=3, sentences=sentences
    )

    wers = await _compare("openai tts", samples, _cloud_asrs())

    assert max(wers) < 0.2
