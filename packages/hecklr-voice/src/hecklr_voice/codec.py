"""Telephone line simulation: 300-3400 Hz band-pass, then a narrowband codec at 8 kHz.

G.711 mu-law is implemented here bit-exactly in numpy. AMR-NB and low-bitrate Opus go through
``ffmpeg`` (``libopencore_amrnb`` / ``libopus``) when available; a persona asking for a codec this
machine cannot run is refused up front (:func:`require_line`), never silently sent clean.
Every path returns 16 kHz audio of the input's length, re-aligned to it, so a codec's
algorithmic delay never shows up as agent latency.
"""

import shutil
import subprocess
from functools import lru_cache
from typing import Final

import numpy as np
from scipy.signal import butter, correlate, sosfiltfilt

from hecklr_schemas import LineCodec
from hecklr_voice.audio import SAMPLE_RATE, Audio, Pcm16, from_pcm16, resample, to_pcm16
from hecklr_voice.errors import VoiceConfigError, VoiceProviderError

NARROWBAND_RATE: Final = 8_000
_BIAS: Final = 0x84
_CLIP: Final = 32_635
_MAX_ALIGN_LAG: Final = 800
"""Longest codec delay searched for when re-aligning, in 8 kHz samples (100 ms)."""

_FFMPEG_CODECS: Final[dict[LineCodec, tuple[str, list[str], str]]] = {
    LineCodec.AMR_NB: ("libopencore_amrnb", ["-b:a", "12.2k"], "amr"),
    LineCodec.OPUS_LOW: ("libopus", ["-b:a", "8k", "-application", "voip"], "ogg"),
}


def mulaw_encode(pcm: Pcm16) -> np.ndarray:
    """G.711 mu-law encode 16-bit linear PCM to 8-bit codes (ITU-T G.711, bit-exact)."""
    linear = pcm.astype(np.int32)
    sign = (linear < 0).astype(np.int32)
    magnitude = np.minimum(np.abs(linear), _CLIP) + _BIAS
    exponent = np.clip(np.floor(np.log2(magnitude)).astype(np.int32) - 7, 0, 7)
    mantissa = (magnitude >> (exponent + 3)) & 0x0F
    return (~((sign << 7) | (exponent << 4) | mantissa) & 0xFF).astype(np.uint8)


def mulaw_decode(codes: np.ndarray) -> Pcm16:
    """G.711 mu-law decode 8-bit codes to 16-bit linear PCM."""
    inverted = ~codes.astype(np.int32) & 0xFF
    exponent = (inverted >> 4) & 0x07
    mantissa = inverted & 0x0F
    magnitude = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return np.where(inverted & 0x80, -magnitude, magnitude).astype(np.int16)


def telephone_bandpass(audio: Audio) -> Audio:
    """300-3400 Hz band-pass; zero-phase, so speech edges do not move in time."""
    if len(audio) < 30:
        return audio
    sos = butter(4, (300.0, 3400.0), btype="bandpass", fs=SAMPLE_RATE, output="sos")
    return np.asarray(sosfiltfilt(sos, audio.astype(np.float64)), dtype=np.float32)


def g711_mulaw(audio: Audio) -> Audio:
    """The full G.711 line: band-pass, 8 kHz, mu-law round trip, back to 16 kHz."""
    narrow = to_pcm16(resample(telephone_bandpass(audio), SAMPLE_RATE, NARROWBAND_RATE))
    decoded = from_pcm16(mulaw_decode(mulaw_encode(narrow)))
    return _fit(resample(decoded, NARROWBAND_RATE, SAMPLE_RATE), len(audio))


@lru_cache(maxsize=1)
def _ffmpeg_encoders() -> frozenset[str]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return frozenset()
    listing = subprocess.run(  # noqa: S603 -- fixed argument list, no user input
        [ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, check=False
    )
    rows = [line.split() for line in listing.stdout.splitlines()]
    return frozenset(row[1] for row in rows if len(row) > 1)


def line_available(line: LineCodec) -> bool:
    """Whether this machine can simulate ``line``."""
    if line not in _FFMPEG_CODECS:
        return True
    return _FFMPEG_CODECS[line][0] in _ffmpeg_encoders()


def require_line(line: LineCodec) -> None:
    """Refuse a line codec this machine cannot simulate."""
    if not line_available(line):
        encoder = _FFMPEG_CODECS[line][0]
        raise VoiceConfigError(f"line {line.value!r} needs ffmpeg built with {encoder}")


def _ffmpeg(args: list[str], data: bytes) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise VoiceConfigError("this line codec needs ffmpeg on PATH")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", *args]
    done = subprocess.run(command, input=data, capture_output=True, check=False)  # noqa: S603 -- fixed codec arguments, audio on stdin
    if done.returncode != 0:
        raise VoiceProviderError(f"ffmpeg failed: {done.stderr.decode(errors='replace')[:300]}")
    return done.stdout


def ffmpeg_codec(audio: Audio, line: LineCodec) -> Audio:
    """Band-pass, then an encode/decode round trip through ffmpeg at 8 kHz."""
    encoder, options, container = _FFMPEG_CODECS[line]
    narrow = to_pcm16(resample(telephone_bandpass(audio), SAMPLE_RATE, NARROWBAND_RATE))
    raw = ["-f", "s16le", "-ar", str(NARROWBAND_RATE), "-ac", "1"]
    encode = [*raw, "-i", "pipe:0", "-c:a", encoder, *options, "-f", container, "pipe:1"]
    encoded = _ffmpeg(encode, narrow.tobytes())
    decoded_bytes = _ffmpeg(["-f", container, "-i", "pipe:0", *raw, "pipe:1"], encoded)
    decoded = np.frombuffer(decoded_bytes, dtype="<i2").astype(np.int16)
    aligned = _align(from_pcm16(narrow), from_pcm16(decoded))
    return _fit(resample(aligned, NARROWBAND_RATE, SAMPLE_RATE), len(audio))


def apply_line(audio: Audio, line: LineCodec) -> Audio:
    """Simulate ``line`` on 16 kHz audio; ``wideband`` is the identity."""
    match line:
        case LineCodec.WIDEBAND:
            return audio
        case LineCodec.G711_MULAW_8K:
            return g711_mulaw(audio)
        case LineCodec.AMR_NB | LineCodec.OPUS_LOW:
            return ffmpeg_codec(audio, line)


def _align(reference: Audio, decoded: Audio) -> Audio:
    """Undo the codec's delay: shift ``decoded`` back by the lag that best matches ``reference``."""
    if not np.any(reference) or len(decoded) == 0:
        return _fit(decoded, len(reference))
    window = decoded[: len(reference) + _MAX_ALIGN_LAG]
    scores = correlate(window, reference, mode="full")[len(reference) - 1 :]
    lag = int(np.argmax(scores[: _MAX_ALIGN_LAG + 1]))
    return _fit(decoded[lag:], len(reference))


def _fit(audio: Audio, length: int) -> Audio:
    """Trim or zero-pad to ``length`` samples."""
    if len(audio) >= length:
        return audio[:length].astype(np.float32)
    return np.concatenate([audio, np.zeros(length - len(audio), dtype=np.float32)])
