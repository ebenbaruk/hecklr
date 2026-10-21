"""Per-trial call recordings on the trial timeline: caller, agent and a stereo mix.

Sample ``n`` of every track is trial time ``n / 16`` ms, so an event's ``start_ms`` and the
audio it describes line up in the report's waveform without any offset bookkeeping.
"""

import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import numpy as np

from hecklr_voice.audio import SAMPLE_RATE, Pcm16

CALLER_WAV: Final = "caller.wav"
AGENT_WAV: Final = "agent.wav"
MIX_WAV: Final = "mix.wav"


class Track:
    """A growable int16 track written at absolute sample positions (later writes win)."""

    def __init__(self, capacity: int = SAMPLE_RATE * 30) -> None:
        self._data = np.zeros(capacity, dtype=np.int16)
        self.length = 0

    def write(self, position: int, samples: Pcm16) -> None:
        """Place ``samples`` starting at ``position``; gaps stay silent."""
        if position < 0:
            raise ValueError(f"track position must be >= 0, got {position}")
        end = position + len(samples)
        if end > len(self._data):
            grown = np.zeros(max(end, 2 * len(self._data)), dtype=np.int16)
            grown[: self.length] = self._data[: self.length]
            self._data = grown
        self._data[position:end] = samples
        self.length = max(self.length, end)

    def read(self, start: int, end: int) -> Pcm16:
        """A copy of samples ``[start, end)``; beyond the written length reads as silence."""
        start, end = max(0, start), max(start, end)
        out = np.zeros(end - start, dtype=np.int16)
        available = max(0, min(end, self.length) - start)
        out[:available] = self._data[start : start + available]
        return out

    def samples(self) -> Pcm16:
        """Everything written so far."""
        return self._data[: self.length].copy()


@dataclass(frozen=True, slots=True)
class RecordingPaths:
    """Where one call's recordings go, relative to the run directory (as events cite them)."""

    directory: str

    @property
    def caller(self) -> str:
        """The caller track, as sent (after impairments)."""
        return f"{self.directory}/{CALLER_WAV}"

    @property
    def agent(self) -> str:
        """The agent track, as received."""
        return f"{self.directory}/{AGENT_WAV}"

    @property
    def mix(self) -> str:
        """Stereo: caller left, agent right."""
        return f"{self.directory}/{MIX_WAV}"


class CallRecorder:
    """Both sides of one call."""

    def __init__(self) -> None:
        self.caller = Track()
        self.agent = Track()

    def write(self, run_dir: Path, paths: RecordingPaths) -> None:
        """Write the caller, agent and stereo mix WAVs (blocking: call it from a thread)."""
        length = max(self.caller.length, self.agent.length)
        caller, agent = self.caller.read(0, length), self.agent.read(0, length)
        (run_dir / paths.directory).mkdir(parents=True, exist_ok=False)
        _write_wav(run_dir / paths.caller, caller, channels=1)
        _write_wav(run_dir / paths.agent, agent, channels=1)
        _write_wav(run_dir / paths.mix, np.column_stack([caller, agent]).reshape(-1), channels=2)


def _write_wav(path: Path, samples: Pcm16, *, channels: int) -> None:
    with wave.open(str(path), "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(samples.astype("<i2").tobytes())
