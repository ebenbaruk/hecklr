"""Real local models (extra ``local``): Kokoro speaks, faster-whisper listens, Silero detects.

Marked ``live``: the first run downloads the models into ``.hecklr/models`` (several hundred MB).
Skipped when the extra is not installed or the download is unreachable.
"""

import importlib.util

import numpy as np
import pytest

from hecklr_voice.asr import WhisperASR, measure_asr, reference_samples
from hecklr_voice.audio import FRAME_SAMPLES, to_pcm16
from hecklr_voice.errors import VoiceProviderError
from hecklr_voice.impair import ImpairmentChain
from hecklr_voice.models import DEFAULT_MODELS_DIR
from hecklr_voice.tts import SpeechRequest
from hecklr_voice.tts_kokoro import KokoroTTS
from hecklr_voice.vad import SileroVad

pytestmark = [
    pytest.mark.live,
    pytest.mark.anyio,
    pytest.mark.skipif(
        importlib.util.find_spec("kokoro_onnx") is None
        or importlib.util.find_spec("faster_whisper") is None,
        reason="needs the 'local' extra",
    ),
]

SENTENCES = (
    "Your boiler visit is booked for Thursday at nine in the morning.",
    "Could you confirm your customer number and postcode please?",
    "Is there anything else I can help you with today?",
)


async def _kokoro_or_skip(text: str) -> np.ndarray:
    try:
        return await KokoroTTS(models_dir=DEFAULT_MODELS_DIR).synthesize(
            SpeechRequest(text=text, voice="af_sarah")
        )
    except VoiceProviderError as exc:
        pytest.skip(f"Kokoro model unavailable: {exc}")


async def test_kokoro_speech_is_heard_by_whisper_with_low_wer() -> None:
    await _kokoro_or_skip("warm up")
    tts = KokoroTTS(models_dir=DEFAULT_MODELS_DIR)
    clean = await reference_samples(
        tts, ImpairmentChain(), voice="af_sarah", seed=1, sentences=SENTENCES
    )
    quality = await measure_asr(WhisperASR("tiny.en"), clean, seed=1)
    assert quality.wer.value < 0.25, quality


async def test_silero_detects_kokoro_speech_and_not_silence() -> None:
    speech = to_pcm16(await _kokoro_or_skip("Hello, thanks for calling Acme maintenance."))
    vad = SileroVad(models_dir=DEFAULT_MODELS_DIR)
    frames = speech[: len(speech) // FRAME_SAMPLES * FRAME_SAMPLES].reshape(-1, FRAME_SAMPLES)
    verdicts = [await vad.is_speech(frame) for frame in frames]
    assert np.mean(verdicts) > 0.5
    silence = SileroVad(models_dir=DEFAULT_MODELS_DIR)
    quiet = [await silence.is_speech(np.zeros(FRAME_SAMPLES, dtype=np.int16)) for _ in range(20)]
    assert not any(quiet)
