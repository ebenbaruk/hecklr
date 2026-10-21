"""Audio formats on the wire: the harness format, G.711 mu-law, and a low-delay resampler.

The harness speaks one format (ADR 0009a): mono 16-bit PCM at 16 kHz in 20 ms frames. Agents
speak whatever their transport carries, so adapters convert at the edge. Conversion must not
move audio in time: every timing Hecklr reports is read from sample positions, so the resampler
keeps output sample ``j`` exactly at input time ``j * rate_in / rate_out`` and holds back only a
few input samples of look-ahead (well under a millisecond at 16 kHz). General-purpose streaming
resamplers buffer tens of milliseconds, which would show up as fake agent latency.
"""

from typing import Final

import numpy as np
import numpy.typing as npt

SAMPLE_RATE: Final = 16_000
"""The harness rate: adapters take and deliver 16 kHz audio."""
FRAME_MS: Final = 20
FRAME_SAMPLES: Final = SAMPLE_RATE * FRAME_MS // 1000
SAMPLES_PER_MS: Final = SAMPLE_RATE // 1000
MULAW_RATE: Final = 8_000

Pcm16 = npt.NDArray[np.int16]

_MULAW_BIAS: Final = 0x84
_MULAW_CLIP: Final = 32_635
_HALF_WIDTH: Final = 8
"""Kernel half-width in zero crossings: the resampler's look-ahead, in input samples."""


def pcm16(data: bytes) -> Pcm16:
    """View 16-bit little-endian bytes as samples (copied, so the result is writable)."""
    if len(data) % 2:
        raise ValueError(f"16-bit PCM needs an even byte count, got {len(data)}")
    return np.frombuffer(data, dtype="<i2").astype(np.int16)


def mulaw_encode(samples: Pcm16) -> bytes:
    """Encode 16-bit PCM as G.711 mu-law bytes."""
    values = samples.astype(np.int32)
    sign = np.where(values < 0, 0x80, 0)
    magnitude = np.minimum(np.abs(values), _MULAW_CLIP) + _MULAW_BIAS
    exponent = np.floor(np.log2(magnitude)).astype(np.int32) - 7
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    codes: bytes = (~(sign | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8).tobytes()
    return codes


def _mulaw_table() -> Pcm16:
    codes = ~np.arange(256, dtype=np.int32) & 0xFF
    exponent = (codes >> 4) & 0x07
    magnitude = (((codes & 0x0F) << 3) + _MULAW_BIAS) << exponent
    return np.where(codes & 0x80, _MULAW_BIAS - magnitude, magnitude - _MULAW_BIAS).astype(np.int16)


_MULAW_DECODE: Final = _mulaw_table()


def mulaw_decode(data: bytes) -> Pcm16:
    """Decode G.711 mu-law bytes to 16-bit PCM."""
    return _MULAW_DECODE[np.frombuffer(data, dtype=np.uint8)]


class StreamResampler:
    """Converts a mono 16-bit stream between sample rates, chunk by chunk, without drift.

    A windowed-sinc interpolator evaluated at exact rational positions: output sample ``j`` is
    the input signal at ``j * rate_in / rate_out``. It waits for :data:`_HALF_WIDTH` input
    samples past that position (scaled up when decimating, to widen the anti-alias filter), so
    its only delay is that look-ahead: 1 ms at 8 kHz, 0.5 ms at 16 kHz.
    """

    def __init__(self, rate_in: int, rate_out: int) -> None:
        if rate_in <= 0 or rate_out <= 0:
            raise ValueError(f"sample rates must be positive, got {rate_in} -> {rate_out}")
        self.rate_in = rate_in
        self.rate_out = rate_out
        self._cutoff = min(1.0, rate_out / rate_in)
        self._reach = int(np.ceil(_HALF_WIDTH / self._cutoff))
        self._history = np.zeros(self._reach, dtype=np.float64)
        """Input samples kept for the kernel; ``_history[0]`` is input index ``_base``."""
        self._base = -self._reach
        self._produced = 0

    def process(self, samples: Pcm16) -> Pcm16:
        """Resample one chunk; returns every output sample its input now determines."""
        if self.rate_in == self.rate_out:
            return samples
        self._history = np.concatenate([self._history, samples.astype(np.float64)])
        available = self._base + len(self._history)
        # Output j needs input up to floor(j * rate_in / rate_out) + reach.
        last = ((available - self._reach) * self.rate_out - 1) // self.rate_in
        count = max(0, last - self._produced + 1)
        if count == 0:
            return np.zeros(0, dtype=np.int16)
        positions = (
            np.arange(self._produced, self._produced + count, dtype=np.float64)
            * self.rate_in
            / self.rate_out
        )
        out = self._interpolate(positions)
        self._produced += count
        # The next output's first tap is floor(position) - reach + 1; nothing before it is needed.
        next_center = self._produced * self.rate_in // self.rate_out
        drop = max(0, next_center - self._reach + 1 - self._base)
        self._history = self._history[drop:]
        self._base += drop
        return np.clip(np.rint(out), -32768, 32767).astype(np.int16)

    def _interpolate(self, positions: npt.NDArray[np.float64]) -> npt.NDArray[np.float64]:
        centers = np.floor(positions).astype(np.int64)
        offsets = np.arange(-self._reach + 1, self._reach + 1, dtype=np.int64)
        taps = centers[:, None] + offsets[None, :]
        distance = positions[:, None] - taps
        x = distance * self._cutoff
        window = 0.5 + 0.5 * np.cos(np.pi * np.clip(distance / self._reach, -1.0, 1.0))
        kernel = np.sinc(x) * window
        kernel /= kernel.sum(axis=1, keepdims=True)
        values = self._history[taps - self._base]
        result: npt.NDArray[np.float64] = (kernel * values).sum(axis=1)
        return result


class ToHarness:
    """Brings agent audio to 16 kHz, following the rate it arrives at.

    Some agents change rate mid-call (a Pipecat pipeline whose TTS runs at 24 kHz announces it
    on every frame), so the resampler is rebuilt when the rate changes.
    """

    def __init__(self) -> None:
        self._resampler = StreamResampler(SAMPLE_RATE, SAMPLE_RATE)

    def __call__(self, samples: Pcm16, sample_rate: int) -> Pcm16:
        """``samples`` at ``sample_rate``, converted to the harness rate."""
        if sample_rate != self._resampler.rate_in:
            self._resampler = StreamResampler(sample_rate, SAMPLE_RATE)
        return self._resampler.process(samples)
