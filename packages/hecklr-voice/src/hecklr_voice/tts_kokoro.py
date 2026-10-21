"""Kokoro TTS run locally through ONNX (extra ``local``), with the model downloaded on first use.

Kokoro (Apache-2.0 weights) is driven through ``kokoro-onnx``, whose phonemizer uses espeak-ng
(GPL): that is why it is an optional extra and never a base dependency.
"""

import importlib
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from hecklr_voice.audio import Audio, resample
from hecklr_voice.errors import VoiceConfigError
from hecklr_voice.models import ensure_model_file, onnx_session, require_extra
from hecklr_voice.pool import run_dsp
from hecklr_voice.tts import SpeechRequest

_RELEASE: Final = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MODEL_FILE: Final = "kokoro-v1.0.onnx"
VOICES_FILE: Final = "voices-v1.0.bin"
_RATE_RANGE: Final = (0.5, 2.0)


@lru_cache(maxsize=2)
def _kokoro(model_path: str, voices_path: str) -> Any:
    # One model load per worker process, reused by every utterance that worker synthesizes.
    session = onnx_session(Path(model_path))
    return importlib.import_module("kokoro_onnx").Kokoro.from_session(session, voices_path)


def kokoro_speech(model_path: str, voices_path: str, request: SpeechRequest) -> Audio:
    """Pool job: synthesize with Kokoro (24 kHz) and resample to the harness rate."""
    samples, sample_rate = _kokoro(model_path, voices_path).create(
        request.text, voice=request.voice, speed=request.rate, lang=request.language.lower()
    )
    return resample(samples, int(sample_rate), 16_000)


class KokoroTTS:
    """Local Kokoro voices (``af_sarah``, ``am_michael``, ...); models kept in ``models_dir``."""

    def __init__(self, *, models_dir: Path) -> None:
        require_extra("kokoro_onnx", "the kokoro TTS provider")
        self._dir = models_dir / "kokoro"

    @property
    def name(self) -> str:
        """``kokoro/v1.0``."""
        return "kokoro/v1.0"

    async def synthesize(self, request: SpeechRequest) -> Audio:
        """Download the model if needed, then synthesize in a worker process."""
        low, high = _RATE_RANGE
        if not low <= request.rate <= high:
            raise VoiceConfigError(f"kokoro supports rate {low}-{high}, got {request.rate}")
        if request.pitch_semitones != 0:
            raise VoiceConfigError("kokoro cannot shift pitch; set pitch_semitones: 0")
        model = await ensure_model_file(f"{_RELEASE}/{MODEL_FILE}", self._dir / MODEL_FILE)
        voices = await ensure_model_file(f"{_RELEASE}/{VOICES_FILE}", self._dir / VOICES_FILE)
        return await run_dsp(kokoro_speech, str(model), str(voices), request)
