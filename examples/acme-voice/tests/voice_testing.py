"""Test doubles for the voice agent's models and transport; conftest puts this on ``sys.path``.

Unit tests never load a speech model, and Hecklr's synthetic caller speech carries no words a
recognizer could hear (its text tag does not survive the caller's level normalization). So the
agent hears through ``ScriptedASR``, which returns what the test says the caller said, and speaks
through ``hecklr_voice.SyntheticTTS`` (tagged, which the harness's ``FakeASR`` reads exactly over
the clean agent-to-caller link) or ``ToneTTS`` (a fixed-length tone, for timing tests).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

import anyio
import numpy as np
from acme_voice.config import PipelineConfig

from hecklr_voice import SpeechRequest, Transcript
from hecklr_voice.audio import FRAME_SAMPLES, SAMPLE_RATE, Audio, Pcm16, speech_edges

SPEECH = (np.sin(np.arange(FRAME_SAMPLES) / 3) * 8000).astype(np.int16)
"""One 20 ms frame the energy VAD hears as speech."""
OTHER_SPEECH = (np.sin(np.arange(FRAME_SAMPLES) / 7) * 6000).astype(np.int16)
"""Speech that ``ScriptedASR`` tells apart from ``SPEECH``: another utterance."""
QUIET = np.zeros(FRAME_SAMPLES, dtype=np.int16)
OPENING_SAMPLES = 8 * FRAME_SAMPLES
"""Audio that identifies an utterance: its first 160 ms of sound."""


def frames(*runs: tuple[Pcm16, int]) -> Pcm16:
    """Concatenated runs of ``(frame, count)``."""
    return np.concatenate([np.tile(frame, count) for frame, count in runs])


def config(**overrides: Any) -> PipelineConfig:
    """The clean pipeline on the energy VAD with 600 ms endpointing (a turn ends after 30 quiet
    frames), with ``overrides``."""
    return replace(PipelineConfig(vad="energy", end_of_speech_ms=600), **overrides)


class ScriptedASR:
    """Hears the next scripted line in each new utterance, then ``then`` for any more.

    An utterance is recognized by its opening audio, so transcribing a turn twice (early, then
    once it has ended) hears the same line, as a real recognizer would. A caller says more than
    its script when it barges in (the interjection is a turn of its own), so the ears need
    something to hear past the script.
    """

    def __init__(self, lines: Iterable[str], then: str = "") -> None:
        self._lines = list(lines)
        self._then = then
        self._known: dict[bytes, str] = {}
        self.heard: list[int] = []
        """Length in samples of each utterance it was given."""

    @property
    def name(self) -> str:
        return "scripted/test"

    async def transcribe(self, pcm: Pcm16, *, language: str) -> Transcript:
        self.heard.append(len(pcm))
        edges = speech_edges(pcm)
        onset = edges[0] if edges is not None else 0
        opening = pcm[onset : onset + OPENING_SAMPLES].tobytes()
        if opening not in self._known:
            self._known[opening] = self._lines.pop(0) if self._lines else self._then
        return Transcript(self._known[opening], 1.0)


class ToneTTS:
    """``seconds`` of tone for any text."""

    def __init__(self, seconds: float = 1.0) -> None:
        self._samples = int(seconds * SAMPLE_RATE)
        self.said: list[str] = []

    @property
    def name(self) -> str:
        return "tone/test"

    async def synthesize(self, request: SpeechRequest) -> Audio:
        self.said.append(request.text)
        return (0.2 * np.sin(np.arange(self._samples) / 5)).astype(np.float32)


@dataclass
class RecordingMouth:
    """Keeps what the call did to its transport, with the clock reading at the time."""

    clock: "FakeClock"
    played: list[tuple[float, int]] = field(default_factory=list)
    """(time, samples) of each clip sent."""
    cleared: list[float] = field(default_factory=list)
    hung_up: list[float] = field(default_factory=list)
    _sent: anyio.Event = field(default_factory=anyio.Event)

    async def play(self, pcm: Pcm16) -> None:
        self.played.append((self.clock(), len(pcm)))
        self._sent.set()
        self._sent = anyio.Event()

    async def wait_played(self, count: int) -> None:
        """Wait until ``count`` clips have been sent."""
        while len(self.played) < count:
            await self._sent.wait()

    async def clear(self) -> None:
        self.cleared.append(self.clock())

    async def hang_up(self) -> None:
        self.hung_up.append(self.clock())


@dataclass
class FakeClock:
    """A settable clock in milliseconds."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now
