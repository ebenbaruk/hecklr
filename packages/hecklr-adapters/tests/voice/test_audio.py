"""Wire audio: G.711 mu-law against hecklr-voice's reference codec, and the streaming resampler."""

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hecklr_adapters.voice.audio import (
    SAMPLE_RATE,
    StreamResampler,
    ToHarness,
    mulaw_decode,
    mulaw_encode,
    pcm16,
)
from hecklr_voice import codec


def test_mulaw_encode_matches_the_g711_reference_for_every_sample() -> None:
    samples = np.arange(-32768, 32768, dtype=np.int32).astype(np.int16)
    assert mulaw_encode(samples) == codec.mulaw_encode(samples).astype(np.uint8).tobytes()


def test_mulaw_decode_matches_the_g711_reference_for_every_code() -> None:
    codes = np.arange(256, dtype=np.uint8)
    assert np.array_equal(mulaw_decode(codes.tobytes()), codec.mulaw_decode(codes))


def test_pcm16_reads_little_endian_and_refuses_half_samples() -> None:
    assert pcm16(b"\x01\x00\xff\xff").tolist() == [1, -1]
    with pytest.raises(ValueError, match="even"):
        pcm16(b"\x00")


def _tone(rate: int, seconds: float, hz: float = 440.0) -> np.ndarray:
    t = np.arange(int(rate * seconds)) / rate
    return (8000 * np.sin(2 * np.pi * hz * t)).astype(np.int16)


def _stream(resampler: StreamResampler, samples: np.ndarray, chunk: int) -> np.ndarray:
    pieces = [resampler.process(samples[i : i + chunk]) for i in range(0, len(samples), chunk)]
    return np.concatenate(pieces)


@pytest.mark.parametrize(
    ("rate_in", "rate_out"),
    [(8000, 16000), (16000, 8000), (48000, 16000), (16000, 24000), (24000, 16000), (44100, 16000)],
)
def test_resampled_audio_stays_at_its_exact_time(rate_in: int, rate_out: int) -> None:
    """Output sample j is the input signal at j * rate_in / rate_out: no delay, no drift."""
    out = _stream(StreamResampler(rate_in, rate_out), _tone(rate_in, 2.0), rate_in // 50)
    expected = 8000 * np.sin(2 * np.pi * 440.0 * np.arange(len(out)) / rate_out)
    assert np.abs(out[100:].astype(float) - expected[100:]).max() < 40  # -46 dB of the tone
    # Only the kernel's look-ahead (8 samples per zero crossing, scaled when decimating) waits.
    held_back_ms = (2.0 * rate_out - len(out)) / rate_out * 1000
    assert 0 <= held_back_ms <= 1.6


def test_decimating_filters_out_what_the_lower_rate_cannot_carry() -> None:
    alias = _stream(StreamResampler(48000, 16000), _tone(48000, 1.0, hz=12_000), 960)
    assert np.abs(alias[200:]).max() < 0.02 * 8000


@settings(max_examples=25, deadline=None)
@given(chunk=st.integers(1, 2000), rates=st.sampled_from([(8000, 16000), (24000, 16000)]))
def test_chunking_never_changes_the_output(chunk: int, rates: tuple[int, int]) -> None:
    signal = _tone(rates[0], 0.25, hz=300)
    whole = StreamResampler(*rates).process(signal)
    assert np.array_equal(_stream(StreamResampler(*rates), signal, chunk), whole)


def test_same_rate_passes_audio_through_untouched() -> None:
    signal = _tone(SAMPLE_RATE, 0.1)
    assert StreamResampler(SAMPLE_RATE, SAMPLE_RATE).process(signal) is signal


def test_resampler_refuses_impossible_rates() -> None:
    with pytest.raises(ValueError, match="positive"):
        StreamResampler(0, 16000)


def test_to_harness_follows_a_rate_change() -> None:
    convert = ToHarness()
    assert len(convert(_tone(16000, 0.1), 16000)) == 1600
    upsampled = convert(_tone(8000, 0.5), 8000)
    assert 7990 - 16 <= len(upsampled) <= 8000
    resampled = convert(_tone(24000, 0.5), 24000)
    assert 8000 - 32 <= len(resampled) <= 8000
