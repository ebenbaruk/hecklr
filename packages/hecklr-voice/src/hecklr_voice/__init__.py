"""Hecklr voice engine: audio pipeline, impairments, turn-taking, voice metrics.

``VoiceDriver(target, asr=make_asr(config.models.asr))`` implements
``hecklr_schemas.ChannelDriver`` for the voice channel over any ``VoiceTarget``. Build the TTS and
ASR with :func:`make_tts` / :func:`make_asr`; ``hecklr_voice.testing.LoopbackVoiceTarget`` is an
in-process calibration agent. Programs that run calls must guard their entry point with
``if __name__ == "__main__":`` (the DSP worker processes import the main module).
"""

from hecklr_voice.asr import (
    ASR,
    REFERENCE_SENTENCES,
    AsrQuality,
    AsrSample,
    DeepgramASR,
    FakeASR,
    Transcript,
    WhisperASR,
    measure_asr,
    reference_samples,
    wer,
)
from hecklr_voice.call import CallStats, VoiceSettings
from hecklr_voice.codec import line_available
from hecklr_voice.driver import VoiceDriver
from hecklr_voice.errors import VoiceConfigError, VoiceProviderError
from hecklr_voice.impair import ROOMS, ImpairmentChain
from hecklr_voice.noise import NOISES
from hecklr_voice.pool import warm_dsp_pool
from hecklr_voice.providers import TTS_PROVIDERS, make_asr, make_tts
from hecklr_voice.tts import TTS, CachedTTS, SpeechRequest, SyntheticTTS, TtsFactory
from hecklr_voice.tts_cloud import CartesiaTTS, ElevenLabsTTS, OpenAITTS
from hecklr_voice.tts_kokoro import KokoroTTS
from hecklr_voice.turntaking import TurnTakingPolicy
from hecklr_voice.vad import EnergyVad, SileroVad, Vad, VadFactory

__version__ = "0.1.0"

__all__ = [
    "ASR",
    "NOISES",
    "REFERENCE_SENTENCES",
    "ROOMS",
    "TTS",
    "TTS_PROVIDERS",
    "AsrQuality",
    "AsrSample",
    "CachedTTS",
    "CallStats",
    "CartesiaTTS",
    "DeepgramASR",
    "ElevenLabsTTS",
    "EnergyVad",
    "FakeASR",
    "ImpairmentChain",
    "KokoroTTS",
    "OpenAITTS",
    "SileroVad",
    "SpeechRequest",
    "SyntheticTTS",
    "Transcript",
    "TtsFactory",
    "TurnTakingPolicy",
    "Vad",
    "VadFactory",
    "VoiceConfigError",
    "VoiceDriver",
    "VoiceProviderError",
    "VoiceSettings",
    "WhisperASR",
    "line_available",
    "make_asr",
    "make_tts",
    "measure_asr",
    "reference_samples",
    "warm_dsp_pool",
    "wer",
]
