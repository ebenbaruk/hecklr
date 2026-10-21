"""Text to speech: the ``TTS`` Protocol, the deterministic ``SyntheticTTS`` and the output cache.

Every provider returns mono float32 audio at the harness rate (16 kHz); the impairment chain
then turns it into what the agent hears. Providers: ``synthetic`` (tests, CI, calibration),
``kokoro`` (local, extra ``local``), ``cartesia``, ``elevenlabs`` and ``openai`` (HTTP, key-gated).
"""

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

import anyio.to_thread
import numpy as np

from hecklr_voice.audio import Audio, from_pcm16, stream_seed, to_pcm16
from hecklr_voice.pool import run_dsp
from hecklr_voice.synth import synthesize_speech
from hecklr_voice.tag import embed_tag

CACHE_KEY_VERSION: Final = "hecklr-tts-v1"
"""Bump to invalidate every cached utterance when synthesis or the key derivation changes."""


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    """One thing to say, in one voice; also the TTS cache key (with the provider's name)."""

    text: str
    voice: str
    rate: float = 1.0
    pitch_semitones: float = 0.0
    language: str = "en-US"
    """BCP 47 tag of the persona."""


@runtime_checkable
class TTS(Protocol):
    """A speech synthesizer."""

    @property
    def name(self) -> str:
        """Provider and model, e.g. ``cartesia/sonic-3.6``; part of the cache key."""
        ...

    async def synthesize(self, request: SpeechRequest) -> Audio:
        """Mono float32 audio of ``request.text`` at 16 kHz."""
        ...


TtsFactory = Callable[[str], TTS]
"""Builds the TTS for a persona's ``voice.tts.provider``."""


def voice_f0(voice: str) -> float:
    """A stable fundamental frequency for a synthetic voice name, between 95 and 230 Hz."""
    return 95.0 + stream_seed(0, "voice", voice) % 13_500 / 100.0


def synthetic_speech(request: SpeechRequest, tag: bool) -> Audio:
    """Pool job for :class:`SyntheticTTS`: synthesize, quantize and optionally tag."""
    f0 = voice_f0(request.voice) * 2 ** (request.pitch_semitones / 12)
    seed = stream_seed(0, "synthetic-tts", request.voice, request.text)
    pcm = to_pcm16(synthesize_speech(request.text, f0_hz=f0, rate=request.rate, seed=seed))
    return from_pcm16(embed_tag(pcm, request.text) if tag else pcm)


class SyntheticTTS:
    """Deterministic speech-like audio; the same request always gives the same samples.

    With ``tag`` (the default) the text is hidden in the audio's least significant bits, so
    ``FakeASR`` transcribes it exactly when it arrives over a clean link.
    """

    def __init__(self, *, tag: bool = True) -> None:
        self._tag = tag

    @property
    def name(self) -> str:
        """``synthetic``."""
        return "synthetic"

    async def synthesize(self, request: SpeechRequest) -> Audio:
        """Synthesize in a worker process."""
        return await run_dsp(synthetic_speech, request, self._tag)


class CachedTTS:
    """Wraps a TTS with an on-disk cache keyed by (provider, text, voice, settings)."""

    def __init__(self, inner: TTS, cache_dir: Path) -> None:
        self._inner = inner
        self._dir = cache_dir

    @property
    def name(self) -> str:
        """The wrapped provider's name."""
        return self._inner.name

    def path_for(self, request: SpeechRequest) -> Path:
        """Where the audio of ``request`` is cached."""
        key = json.dumps(
            [CACHE_KEY_VERSION, self._inner.name, asdict(request)],
            sort_keys=True,
            ensure_ascii=False,
        )
        return self._dir / f"{hashlib.sha256(key.encode()).hexdigest()}.npy"

    async def synthesize(self, request: SpeechRequest) -> Audio:
        """Return the cached audio, or synthesize and cache it."""
        path = self.path_for(request)
        cached = await anyio.to_thread.run_sync(_load, path)
        if cached is not None:
            return cached
        audio = await self._inner.synthesize(request)
        await anyio.to_thread.run_sync(_store, path, audio)
        return audio


def _load(path: Path) -> Audio | None:
    if not path.exists():
        return None
    loaded: Audio = np.load(path, allow_pickle=False)
    return loaded.astype(np.float32)


def _store(path: Path, audio: Audio) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.stem}.{os.getpid()}.part.npy")
    np.save(partial, audio, allow_pickle=False)
    partial.replace(path)
