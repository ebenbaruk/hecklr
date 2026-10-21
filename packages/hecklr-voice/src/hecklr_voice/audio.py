"""Audio basics shared by every stage: the harness format, frames, levels and seeds.

The harness works in one format end to end: mono, 16 kHz, float32 in [-1, 1] for DSP and 16-bit
little-endian PCM on the wire. Codecs that need 8 kHz convert internally and come back to 16 kHz,
so frames, recordings and timings never change rate mid-call.
"""

import hashlib
import math
from fractions import Fraction
from typing import Final

import numpy as np
import numpy.typing as npt
from scipy.signal import resample_poly

SAMPLE_RATE: Final = 16_000
FRAME_MS: Final = 20
FRAME_SAMPLES: Final = SAMPLE_RATE * FRAME_MS // 1000
SAMPLES_PER_MS: Final = SAMPLE_RATE // 1000

REFERENCE_LEVEL_DBFS: Final = -26.0
"""Active speech level every caller utterance is normalized to (ITU-T nominal telephone level)."""

SPEECH_AMPLITUDE: Final = 10 ** (-50 / 20)
"""Sample magnitude above which audio counts as sound when locating speech edges in a frame."""

Audio = npt.NDArray[np.float32]
Pcm16 = npt.NDArray[np.int16]


def to_pcm16(audio: Audio) -> Pcm16:
    """Clip to [-1, 1] and quantize to int16 (round half to even, like every DSP library)."""
    return np.round(np.clip(audio, -1.0, 32767 / 32768) * 32768).astype(np.int16)


def from_pcm16(pcm: Pcm16) -> Audio:
    """Exact inverse of :func:`to_pcm16` for in-range values."""
    return (pcm.astype(np.float32) / np.float32(32768)).astype(np.float32)


def pcm_bytes(audio: Audio) -> bytes:
    """Float audio as little-endian 16-bit PCM bytes, the ``AudioLink`` wire format."""
    return to_pcm16(audio).astype("<i2").tobytes()


def bytes_to_pcm16(data: bytes) -> Pcm16:
    """Little-endian 16-bit PCM bytes as an int16 array."""
    if len(data) % 2:
        raise ValueError(f"16-bit PCM needs an even byte count, got {len(data)}")
    return np.frombuffer(data, dtype="<i2").astype(np.int16)


def resample(audio: Audio, rate_in: int, rate_out: int) -> Audio:
    """Band-limited polyphase resampling between two integer rates."""
    if rate_in <= 0 or rate_out <= 0:
        raise ValueError(f"sample rates must be positive, got {rate_in} -> {rate_out}")
    if rate_in == rate_out:
        return audio.astype(np.float32, copy=False)
    ratio = Fraction(rate_out, rate_in)
    out = resample_poly(audio.astype(np.float64), ratio.numerator, ratio.denominator)
    return np.asarray(out, dtype=np.float32)


def pad_to_frames(audio: Audio) -> Audio:
    """Zero-pad to a whole number of 20 ms frames."""
    remainder = len(audio) % FRAME_SAMPLES
    if remainder == 0:
        return audio
    return np.concatenate([audio, np.zeros(FRAME_SAMPLES - remainder, dtype=np.float32)])


def power(audio: Audio) -> float:
    """Mean square of the signal (0 for an empty signal)."""
    return float(np.mean(np.square(audio, dtype=np.float64))) if len(audio) else 0.0


def active_power(audio: Audio, *, floor_db: float = 30.0) -> float:
    """Mean power over the 20 ms frames within ``floor_db`` of the loudest one.

    A simplified ITU-T P.56 active level: pauses between words and sentences do not dilute the
    speech level, so a hesitation pause never changes the SNR the noise is mixed at.
    """
    frames = len(audio) // FRAME_SAMPLES
    if frames == 0:
        return power(audio)
    blocks = audio[: frames * FRAME_SAMPLES].astype(np.float64).reshape(frames, FRAME_SAMPLES)
    frame_power = np.mean(np.square(blocks), axis=1)
    loudest = float(frame_power.max())
    if loudest == 0.0:
        return 0.0
    active = frame_power[frame_power >= loudest * 10 ** (-floor_db / 10)]
    return float(active.mean())


def db(value: float) -> float:
    """Power ratio in decibels; ``-inf`` for zero."""
    return float(10 * np.log10(value)) if value > 0 else float("-inf")


def normalize_active_level(audio: Audio, level_dbfs: float = REFERENCE_LEVEL_DBFS) -> Audio:
    """Scale so the active speech level is ``level_dbfs``; silence is returned unchanged."""
    level = active_power(audio)
    if level == 0.0:
        return audio
    gain = math.sqrt(10 ** (level_dbfs / 10) / level)
    return (audio * np.float32(gain)).astype(np.float32)


def speech_edges(pcm: Pcm16 | Audio, threshold: float = SPEECH_AMPLITUDE) -> tuple[int, int] | None:
    """Index of the first sound sample and one past the last, or ``None`` if all quiet.

    Both sides of a call use this one rule, so a sample-accurate edge measured by the harness is
    the same edge the loopback agent reacts to.
    """
    scale = 32768.0 if pcm.dtype == np.int16 else 1.0
    loud = np.flatnonzero(np.abs(pcm.astype(np.float32)) >= threshold * scale)
    if len(loud) == 0:
        return None
    return int(loud[0]), int(loud[-1]) + 1


def frame_dbfs(pcm: Pcm16) -> float:
    """RMS level of one int16 frame in dBFS (``-inf`` for digital silence)."""
    if len(pcm) == 0:
        return float("-inf")
    mean_square = float(np.mean(np.square(pcm, dtype=np.float64))) / (32768.0 * 32768.0)
    return db(mean_square)


def stream_seed(seed: int, *keys: str | int) -> int:
    """A 63-bit seed for one decision, stable across processes and independent of call order.

    Every random stream of a trial (gaps, noise, loss, ...) gets its own seed from the trial seed
    and a key, so adding a decision never shifts the numbers of another.
    """
    material = ":".join([str(seed), *(str(key) for key in keys)]).encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big") >> 1


def stream_rng(seed: int, *keys: str | int) -> np.random.Generator:
    """A numpy generator for one named random stream of a trial (see :func:`stream_seed`)."""
    return np.random.default_rng(stream_seed(seed, *keys))
