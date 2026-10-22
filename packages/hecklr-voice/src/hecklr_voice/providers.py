"""Build TTS and ASR providers from configuration names.

A persona's ``voice.tts.provider`` picks the TTS; ``models.asr`` in ``hecklr.yaml`` (a
``provider/model`` name) picks the ASR. Cloud providers read their API key from the environment
and refuse to start without it; local ones need the ``local`` extra.
"""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import httpx2

from hecklr_voice.asr import ASR, DeepgramASR, FakeASR, WhisperASR
from hecklr_voice.asr_assemblyai import AssemblyAIASR, AssemblyAIBatchASR
from hecklr_voice.errors import VoiceConfigError
from hecklr_voice.models import DEFAULT_MODELS_DIR
from hecklr_voice.tts import TTS, CachedTTS, SyntheticTTS
from hecklr_voice.tts_cloud import CLOUD_PROVIDERS
from hecklr_voice.tts_kokoro import KokoroTTS

TTS_PROVIDERS: Final = ("synthetic", "kokoro", *CLOUD_PROVIDERS)


def make_tts(
    provider: str,
    *,
    cache_dir: Path | None = None,
    models_dir: Path = DEFAULT_MODELS_DIR,
    environ: Mapping[str, str] | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> TTS:
    """The TTS named ``provider``, wrapped in the on-disk cache when ``cache_dir`` is set.

    Raises:
        VoiceConfigError: unknown provider, missing API key, or the ``local`` extra not installed.
    """
    env = os.environ if environ is None else environ
    tts: TTS
    if provider == "synthetic":
        tts = SyntheticTTS()
    elif provider == "kokoro":
        tts = KokoroTTS(models_dir=models_dir)
    elif provider in CLOUD_PROVIDERS:
        tts = CLOUD_PROVIDERS[provider].from_environ(env, transport=transport)
    else:
        known = ", ".join(TTS_PROVIDERS)
        raise VoiceConfigError(f"unknown TTS provider {provider!r}; known: {known}")
    return CachedTTS(tts, cache_dir) if cache_dir is not None else tts


def make_asr(
    model: str,
    *,
    models_dir: Path = DEFAULT_MODELS_DIR,
    environ: Mapping[str, str] | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> ASR:
    """The ASR named by ``models.asr``.

    ``fake/*``, ``faster-whisper/<size>``, ``deepgram/<model>``, ``assemblyai/<speech model>``
    (streaming, e.g. ``assemblyai/universal-streaming-english``) or
    ``assemblyai-batch/<speech model>`` (pre-recorded, e.g. ``assemblyai-batch/universal-2``).

    Raises:
        VoiceConfigError: unknown provider, missing API key, or the ``local`` extra not installed.
    """
    provider, _, name = model.partition("/")
    if not name:
        raise VoiceConfigError(f"an ASR model is 'provider/model', got {model!r}")
    env = os.environ if environ is None else environ
    match provider:
        case "fake":
            return FakeASR()
        case "faster-whisper":
            return WhisperASR(name, models_dir=models_dir)
        case "deepgram":
            return DeepgramASR.from_environ(env, name, transport=transport)
        case "assemblyai":
            return AssemblyAIASR.from_environ(env, name)
        case "assemblyai-batch":
            return AssemblyAIBatchASR.from_environ(env, name, transport=transport)
        case _:
            known = "fake, faster-whisper, deepgram, assemblyai, assemblyai-batch"
            raise VoiceConfigError(f"unknown ASR provider {provider!r}; known: {known}")
