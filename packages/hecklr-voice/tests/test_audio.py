"""Audio basics: PCM conversion, levels, edges, seeds, synthesis and the LSB text tag."""

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from hecklr_voice.audio import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
    active_power,
    bytes_to_pcm16,
    db,
    frame_dbfs,
    from_pcm16,
    normalize_active_level,
    pad_to_frames,
    pcm_bytes,
    resample,
    speech_edges,
    stream_seed,
    to_pcm16,
)
from hecklr_voice.synth import phrases, synthesize_speech
from hecklr_voice.tag import embed_tag, extract_tag


def _tone(seconds: float, hz: float = 440.0, amplitude: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    return (amplitude * np.sin(2 * np.pi * hz * t)).astype(np.float32)


@given(st.lists(st.integers(-32768, 32767), max_size=64))
def test_pcm16_round_trip_is_exact(values: list[int]) -> None:
    pcm = np.array(values, dtype=np.int16)
    assert np.array_equal(to_pcm16(from_pcm16(pcm)), pcm)
    assert np.array_equal(bytes_to_pcm16(pcm_bytes(from_pcm16(pcm))), pcm)


def test_bytes_to_pcm16_rejects_odd_lengths() -> None:
    with pytest.raises(ValueError, match="even byte count"):
        bytes_to_pcm16(b"\x00\x01\x02")


def test_to_pcm16_clips_out_of_range_audio() -> None:
    pcm = to_pcm16(np.array([2.0, -2.0], dtype=np.float32))
    assert pcm.tolist() == [32767, -32768]


def test_resample_keeps_duration_and_frequency() -> None:
    tone = _tone(1.0, hz=300.0)
    narrow = resample(tone, SAMPLE_RATE, 8_000)
    assert len(narrow) == 8_000
    spectrum = np.abs(np.fft.rfft(narrow))
    assert np.argmax(spectrum) * 8_000 / len(narrow) == pytest.approx(300.0, abs=2.0)
    assert resample(tone, SAMPLE_RATE, SAMPLE_RATE) is not None
    with pytest.raises(ValueError, match="positive"):
        resample(tone, 0, 8_000)


def test_pad_to_frames_rounds_up_to_whole_frames() -> None:
    assert len(pad_to_frames(np.zeros(1, dtype=np.float32))) == FRAME_SAMPLES
    assert len(pad_to_frames(np.zeros(FRAME_SAMPLES, dtype=np.float32))) == FRAME_SAMPLES


def test_active_power_ignores_pauses() -> None:
    speech = _tone(0.5)
    with_pause = np.concatenate([speech, np.zeros(SAMPLE_RATE, dtype=np.float32), speech])
    assert active_power(with_pause) == pytest.approx(active_power(speech), rel=1e-3)
    assert active_power(np.zeros(100, dtype=np.float32)) == 0.0
    assert active_power(np.zeros(4000, dtype=np.float32)) == 0.0


def test_normalize_active_level_hits_the_target() -> None:
    normalized = normalize_active_level(_tone(1.0, amplitude=0.01), -26.0)
    assert db(active_power(normalized)) == pytest.approx(-26.0, abs=0.01)
    silence = np.zeros(800, dtype=np.float32)
    assert normalize_active_level(silence) is silence


def test_speech_edges_find_the_first_and_last_sound_sample() -> None:
    audio = np.zeros(1000, dtype=np.float32)
    audio[100:300] = 0.2
    assert speech_edges(audio) == (100, 300)
    assert speech_edges(to_pcm16(audio)) == (100, 300)
    assert speech_edges(np.zeros(10, dtype=np.float32)) is None


def test_frame_dbfs_of_full_scale_and_silence() -> None:
    assert frame_dbfs(np.full(320, 32767, dtype=np.int16)) == pytest.approx(0.0, abs=0.01)
    assert frame_dbfs(np.zeros(320, dtype=np.int16)) == float("-inf")
    assert frame_dbfs(np.zeros(0, dtype=np.int16)) == float("-inf")


def test_stream_seeds_are_stable_and_independent() -> None:
    assert stream_seed(7, "gap", 1) == stream_seed(7, "gap", 1)
    assert stream_seed(7, "gap", 1) != stream_seed(7, "gap", 2)
    assert stream_seed(7, "gap", 1) != stream_seed(8, "gap", 1)
    assert 0 <= stream_seed(7, "x") < 2**63


def test_synthetic_speech_is_deterministic_and_scales_with_text_and_rate() -> None:
    text = "Hello there, I would like to move my boiler visit to next week."
    first = synthesize_speech(text, f0_hz=150, rate=1.0, seed=3)
    assert np.array_equal(first, synthesize_speech(text, f0_hz=150, rate=1.0, seed=3))
    fast = synthesize_speech(text, f0_hz=150, rate=2.0, seed=3)
    longer = synthesize_speech(f"{text} {text}", f0_hz=150, rate=1.0, seed=3)
    assert 1.6 < len(first) / len(fast) < 2.4
    assert 1.8 < len(longer) / len(first) < 2.2
    assert 2.0 < len(first) / SAMPLE_RATE < 5.0


def test_synthetic_speech_is_voiced_in_the_telephone_band() -> None:
    audio = synthesize_speech("one two three four five six", f0_hz=120, rate=1.0, seed=1)
    spectrum = np.abs(np.fft.rfft(audio)) ** 2
    freqs = np.fft.rfftfreq(len(audio), 1 / SAMPLE_RATE)
    in_band = spectrum[(freqs > 100) & (freqs < 3400)].sum()
    assert in_band / spectrum.sum() > 0.95


def test_synthetic_speech_only_pauses_at_punctuation() -> None:
    audio = synthesize_speech("this phrase has no pauses inside", f0_hz=150, rate=1.0, seed=2)
    frames = to_pcm16(audio)[: len(audio) // FRAME_SAMPLES * FRAME_SAMPLES].reshape(
        -1, FRAME_SAMPLES
    )
    assert all(frame_dbfs(frame) > -45 for frame in frames)
    assert len(phrases("Hi. How are you, today?")) == 3
    assert synthesize_speech("...", f0_hz=150, rate=1.0, seed=0).size == 0
    with pytest.raises(ValueError, match="positive"):
        synthesize_speech("hi", f0_hz=0, rate=1.0, seed=0)


@given(st.text(min_size=1, max_size=60))
def test_tag_round_trips_any_text(text: str) -> None:
    pcm = to_pcm16(_tone(0.3, amplitude=0.3))
    tagged = embed_tag(pcm, text)
    assert extract_tag(tagged) == text
    assert np.max(np.abs(tagged.astype(np.int32) - pcm.astype(np.int32))) <= 1


def test_tag_survives_any_cut_holding_one_copy() -> None:
    pcm = embed_tag(to_pcm16(_tone(1.0, amplitude=0.3)), "Your visit is on Tuesday.")
    assert extract_tag(pcm[1234:9000]) == "Your visit is on Tuesday."


def test_tag_absent_from_untagged_or_impaired_audio() -> None:
    pcm = to_pcm16(_tone(1.0, amplitude=0.3))
    assert extract_tag(pcm) is None
    tagged = embed_tag(pcm, "hello")
    assert extract_tag((tagged // 2 * 2).astype(np.int16)) is None
    assert extract_tag(pcm[:10]) is None
    assert np.array_equal(embed_tag(pcm[:20], "too long to fit"), pcm[:20])
    with pytest.raises(ValueError, match="at most"):
        embed_tag(pcm, "x" * 70_000)
