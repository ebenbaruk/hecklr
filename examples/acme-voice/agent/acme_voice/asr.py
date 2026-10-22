"""How the agent hears callers: faster-whisper decoding words only, or any hecklr-voice ASR.

hecklr-voice's ``WhisperASR`` decodes with timestamps. On some utterances of the suite that took
6-9 s instead of 0.4 s every run (reproduced offline on the agent's captured audio: 6.05 s with
timestamps, 0.40 s without, same words; issue #48), which put seconds on the agent's answers. An
agent needs the words only, so it decodes without timestamps; the model, its threads and the
worker pool are hecklr-voice's.
"""

import importlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from hecklr_voice import ASR, Transcript, make_asr
from hecklr_voice.audio import Pcm16, from_pcm16
from hecklr_voice.models import MODEL_THREADS, require_extra
from hecklr_voice.pool import run_dsp

WHISPER = "faster-whisper"


@lru_cache(maxsize=2)
def _model(model: str, download_root: str) -> Any:  # faster-whisper is untyped
    # One model load per worker process, reused for every utterance it transcribes.
    module = importlib.import_module("faster_whisper")
    return module.WhisperModel(
        model,
        device="cpu",
        compute_type="int8",
        cpu_threads=MODEL_THREADS,
        download_root=download_root,
    )


def transcribe_words(model: str, download_root: str, pcm: Pcm16, language: str) -> Transcript:
    """Pool job: the words of ``pcm``, greedy and without timestamps."""
    segments, _ = _model(model, download_root).transcribe(
        from_pcm16(pcm),
        language=language.split("-")[0].lower(),
        beam_size=1,
        without_timestamps=True,
    )
    return Transcript(" ".join(segment.text.strip() for segment in segments).strip())


class WordsASR:
    """faster-whisper on CPU (int8), words only; models are kept in ``models_dir/whisper``."""

    def __init__(self, model: str, *, models_dir: Path) -> None:
        require_extra("faster_whisper", "the faster-whisper ASR provider")
        self._model = model
        self._root = str(models_dir / "whisper")

    @property
    def name(self) -> str:
        """``faster-whisper/<model>``, as ``voice.yaml`` names it."""
        return f"{WHISPER}/{self._model}"

    async def transcribe(self, pcm: Pcm16, *, language: str) -> Transcript:
        """Transcribe in a DSP worker process."""
        return await run_dsp(transcribe_words, self._model, self._root, pcm, language)


def make_agent_asr(name: str, *, models_dir: Path) -> ASR:
    """The recognizer ``voice.yaml``'s ``asr_model`` names: ``WordsASR`` for faster-whisper."""
    provider, _, model = name.partition("/")
    if provider == WHISPER and model:
        return WordsASR(model, models_dir=models_dir)
    return make_asr(name, models_dir=models_dir)
