"""The impairment chain: determinism, SNR accuracy, reverb, line codecs and packet loss."""

import itertools

import numpy as np
import pytest

from hecklr_schemas import LineCodec, TtsConfig, VoiceProfile
from hecklr_voice.audio import (
    FRAME_SAMPLES,
    REFERENCE_LEVEL_DBFS,
    SAMPLE_RATE,
    active_power,
    bytes_to_pcm16,
    db,
    from_pcm16,
    power,
    speech_edges,
    to_pcm16,
)
from hecklr_voice.codec import (
    apply_line,
    line_available,
    mulaw_decode,
    mulaw_encode,
    require_line,
    telephone_bandpass,
)
from hecklr_voice.errors import VoiceConfigError
from hecklr_voice.impair import (
    DEFAULT_SPEED_JITTER,
    ROOMS,
    ImpairmentChain,
    Segment,
    mix_at_snr,
    render_clip,
    render_idle,
    room_impulse_response,
    speed_perturb,
)
from hecklr_voice.network import SendJitter, conceal, gilbert_elliott_loss
from hecklr_voice.noise import NOISES, generate_noise
from hecklr_voice.synth import synthesize_speech

SPEECH = synthesize_speech(
    "Hi, I need to move my boiler visit, ideally to next week in the morning.",
    f0_hz=140,
    rate=1.0,
    seed=11,
)


def _audio(clip_pcm: bytes) -> np.ndarray:
    return from_pcm16(bytes_to_pcm16(clip_pcm))


def _clip(chain: ImpairmentChain, seed: int = 5) -> np.ndarray:
    segments = (Segment("speech", SPEECH), Segment.pause(600), Segment("speech", SPEECH[:8000]))
    return _audio(render_clip(segments, chain, seed).pcm)


def test_chain_for_voice_profile_takes_every_field() -> None:
    profile = VoiceProfile(
        tts=TtsConfig(provider="synthetic", voice="a"),
        environment="car_interior",
        snr_db=12,
        reverb="office",
        line=LineCodec.G711_MULAW_8K,
        packet_loss=0.02,
    )
    chain = ImpairmentChain.for_voice(profile)
    assert (chain.environment, chain.snr_db, chain.reverb) == ("car_interior", 12, "office")
    assert (chain.line, chain.packet_loss) == (LineCodec.G711_MULAW_8K, 0.02)
    assert chain.speed_jitter == DEFAULT_SPEED_JITTER


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"environment": "jungle", "snr_db": 5.0}, "unknown environment"),
        ({"environment": "street"}, "needs snr_db"),
        ({"reverb": "cave"}, "unknown reverb"),
        ({"speed_jitter": 0.7}, "speed_jitter"),
    ],
)
def test_chain_refuses_settings_it_cannot_honor(fields: dict[str, object], message: str) -> None:
    with pytest.raises(VoiceConfigError, match=message):
        ImpairmentChain(**fields)  # type: ignore[arg-type]  # deliberately invalid values


def test_render_clip_is_deterministic_in_its_seed() -> None:
    chain = ImpairmentChain(
        speed_jitter=0.03,
        environment="cafe",
        snr_db=5.0,
        reverb="living_room",
        line=LineCodec.G711_MULAW_8K,
        packet_loss=0.05,
    )
    assert np.array_equal(_clip(chain, seed=1), _clip(chain, seed=1))
    assert not np.array_equal(_clip(chain, seed=1), _clip(chain, seed=2))


def test_render_clip_spans_mark_where_each_segment_sounds() -> None:
    segments = (
        Segment("filler", SPEECH[:4000]),
        Segment.pause(500),
        Segment("speech", SPEECH),
    )
    clip = render_clip(segments, ImpairmentChain(), seed=0)
    filler, speech = clip.spans
    assert filler.content == "filler"
    assert speech.content == "speech"
    pause_start = 4000
    assert filler.end <= pause_start
    assert speech.start >= pause_start + 500 * SAMPLE_RATE // 1000
    assert clip.samples % FRAME_SAMPLES == 0
    assert clip.samples >= speech.end
    assert clip.lost_frames == 0


def test_render_clip_normalizes_speech_to_the_reference_level() -> None:
    audio = _clip(ImpairmentChain())
    assert db(active_power(audio)) == pytest.approx(REFERENCE_LEVEL_DBFS, abs=0.1)


@pytest.mark.parametrize("environment", sorted(NOISES))
@pytest.mark.parametrize("snr_db", [0.0, 5.0, 10.0, 20.0])
def test_noise_is_mixed_within_half_a_db_of_the_target_snr(environment: str, snr_db: float) -> None:
    clean = _clip(ImpairmentChain())
    noisy = _clip(ImpairmentChain(environment=environment, snr_db=snr_db))
    measured = db(active_power(clean) / power(noisy - clean))
    assert measured == pytest.approx(snr_db, abs=0.5)


def test_mix_at_snr_leaves_silence_alone() -> None:
    silence = np.zeros(1000, dtype=np.float32)
    assert mix_at_snr(silence, np.ones(1000, dtype=np.float32), 10.0) is silence


@pytest.mark.parametrize("environment", sorted(NOISES))
def test_noise_generators_are_seeded_unit_rms_and_finite(environment: str) -> None:
    first = generate_noise(environment, SAMPLE_RATE, seed=4)
    assert np.array_equal(first, generate_noise(environment, SAMPLE_RATE, seed=4))
    assert not np.array_equal(first, generate_noise(environment, SAMPLE_RATE, seed=5))
    assert np.sqrt(power(first)) == pytest.approx(1.0, rel=1e-3)
    assert np.all(np.isfinite(first))
    assert generate_noise(environment, 0, seed=1).size == 0


def test_car_noise_is_low_frequency_and_construction_is_impulsive() -> None:
    car = generate_noise("car_interior", 4 * SAMPLE_RATE, seed=1)
    spectrum = np.abs(np.fft.rfft(car)) ** 2
    freqs = np.fft.rfftfreq(len(car), 1 / SAMPLE_RATE)
    assert spectrum[freqs < 500].sum() / spectrum.sum() > 0.9
    construction = generate_noise("construction", 4 * SAMPLE_RATE, seed=1)
    crest = np.max(np.abs(construction)) / np.sqrt(power(construction))
    assert crest > 6


def test_speed_perturbation_changes_duration_by_the_factor() -> None:
    faster = speed_perturb(SPEECH, 1.03)
    assert len(faster) == pytest.approx(len(SPEECH) / 1.03, rel=0.01)
    assert speed_perturb(SPEECH, 1.0) is SPEECH


@pytest.mark.parametrize("room", sorted(ROOMS))
def test_reverb_impulse_response_decays_at_its_rt60(room: str) -> None:
    response = room_impulse_response(room, seed=1)
    assert response[0] == 1.0
    tail = response[1:] ** 2
    early = tail[: len(tail) // 10].mean()
    late = tail[-len(tail) // 10 :].mean()
    assert late < early


def test_reverb_keeps_length_and_smears_energy_into_pauses() -> None:
    dry = _clip(ImpairmentChain())
    wet = _clip(ImpairmentChain(reverb="hall"))
    assert len(wet) == len(dry)
    pause = slice(len(SPEECH) + 800, len(SPEECH) + 1600)
    assert power(wet[pause]) > 10 * power(dry[pause]) + 1e-12


def test_mulaw_matches_g711_reference_codes_and_round_trips() -> None:
    linear = np.array([0, 32767, -32768, 1000, -1000], dtype=np.int16)
    codes = mulaw_encode(linear)
    assert codes[:3].tolist() == [0xFF, 0x80, 0x00]
    every_code = np.arange(256, dtype=np.uint8)
    decoded = mulaw_decode(every_code)
    assert np.array_equal(mulaw_decode(mulaw_encode(decoded)), decoded)
    tone = to_pcm16((0.5 * np.sin(2 * np.pi * 440 * np.arange(8000) / 8000)).astype(np.float32))
    error = from_pcm16(tone) - from_pcm16(mulaw_decode(mulaw_encode(tone)))
    assert db(power(from_pcm16(tone)) / power(error)) > 35


def test_g711_line_is_band_limited_and_keeps_timing() -> None:
    line = apply_line(SPEECH, LineCodec.G711_MULAW_8K)
    assert len(line) == len(SPEECH)
    spectrum = np.abs(np.fft.rfft(line)) ** 2
    freqs = np.fft.rfftfreq(len(line), 1 / SAMPLE_RATE)
    assert spectrum[freqs > 4200].sum() / spectrum.sum() < 1e-3
    original, impaired = speech_edges(SPEECH), speech_edges(line, threshold=10 ** (-40 / 20))
    assert original is not None
    assert impaired is not None
    assert abs(original[0] - impaired[0]) < 3 * 16
    assert apply_line(SPEECH, LineCodec.WIDEBAND) is SPEECH
    assert telephone_bandpass(SPEECH[:10]) is not None


@pytest.mark.parametrize("line", [LineCodec.AMR_NB, LineCodec.OPUS_LOW])
def test_ffmpeg_codecs_round_trip_aligned_when_available(line: LineCodec) -> None:
    if not line_available(line):
        pytest.skip(f"ffmpeg with an encoder for {line.value} is not installed")
    out = apply_line(SPEECH, line)
    assert len(out) == len(SPEECH)
    reference = apply_line(SPEECH, LineCodec.G711_MULAW_8K)
    correlation = np.corrcoef(out, reference)[0, 1]
    assert correlation > 0.6


def test_unavailable_codecs_are_refused_up_front(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("hecklr_voice.codec._ffmpeg_encoders", frozenset)
    assert not line_available(LineCodec.AMR_NB)
    assert line_available(LineCodec.G711_MULAW_8K)
    with pytest.raises(VoiceConfigError, match="libopencore_amrnb"):
        require_line(LineCodec.AMR_NB)
    with pytest.raises(VoiceConfigError, match="needs ffmpeg"):
        ImpairmentChain(line=LineCodec.OPUS_LOW)


def test_packet_loss_is_concealed_and_counted() -> None:
    chain = ImpairmentChain(packet_loss=0.2)
    clip = render_clip((Segment("speech", SPEECH),), chain, seed=3)
    assert clip.lost_frames > 0
    clean = render_clip((Segment("speech", SPEECH),), ImpairmentChain(), seed=3)
    assert clip.pcm != clean.pcm
    assert len(clip.pcm) == len(clean.pcm)


@pytest.mark.parametrize(("rate", "burst"), [(0.02, 2.0), (0.1, 2.0), (0.1, 4.0), (0.3, 3.0)])
def test_gilbert_elliott_matches_rate_and_mean_burst(rate: float, burst: float) -> None:
    lost = gilbert_elliott_loss(200_000, rate, burst, np.random.default_rng(1))
    assert lost.mean() == pytest.approx(rate, abs=0.15 * rate)
    edges = np.diff(np.concatenate([[0], lost.astype(int), [0]]))
    bursts = np.flatnonzero(edges == -1) - np.flatnonzero(edges == 1)
    assert bursts.mean() == pytest.approx(burst, rel=0.1)


def test_gilbert_elliott_with_unit_memory_is_independent_loss() -> None:
    rate = 0.1
    lost = gilbert_elliott_loss(200_000, rate, 1 / (1 - rate), np.random.default_rng(2))
    after_loss = lost[1:][lost[:-1]]
    assert after_loss.mean() == pytest.approx(rate, abs=0.01)
    assert lost.mean() == pytest.approx(rate, abs=0.01)


def test_gilbert_elliott_edge_cases() -> None:
    generator = np.random.default_rng(0)
    assert not gilbert_elliott_loss(100, 0.0, 2.0, generator).any()
    with pytest.raises(ValueError, match="mean_burst"):
        gilbert_elliott_loss(10, 0.1, 0.5, generator)
    with pytest.raises(ValueError, match="unreachable"):
        gilbert_elliott_loss(10, 0.9, 1.0, generator)
    with pytest.raises(ValueError, match="loss rate"):
        gilbert_elliott_loss(10, 1.0, 2.0, generator)


def test_concealment_repeats_with_fade_or_zeroes() -> None:
    audio = np.repeat(np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32), FRAME_SAMPLES)
    lost = np.array([False, True, True, False])
    repeated = conceal(audio, lost, "repeat")
    assert repeated[FRAME_SAMPLES] == 0.5
    assert repeated[2 * FRAME_SAMPLES] == 0.25
    assert repeated[3 * FRAME_SAMPLES] == 4.0
    zeroed = conceal(audio, lost, "zero")
    assert zeroed[FRAME_SAMPLES] == 0.0
    with pytest.raises(ValueError, match="loss mask"):
        conceal(audio, lost[:2])


def test_send_jitter_stays_in_range_and_never_reorders_frames() -> None:
    jitter = SendJitter(60, np.random.default_rng(3))
    delays = [jitter.next_delay_ms() for _ in range(5000)]
    assert min(delays) >= 0
    assert max(delays) <= 60
    send_times = [20 * index + delay for index, delay in enumerate(delays)]
    assert all(later >= earlier for earlier, later in itertools.pairwise(send_times))
    assert SendJitter(0, np.random.default_rng(0)).next_delay_ms() == 0.0
    with pytest.raises(ValueError, match="jitter_ms"):
        SendJitter(-1, np.random.default_rng(0))


def test_idle_line_carries_noise_at_the_speech_relative_level() -> None:
    chain = ImpairmentChain(environment="street", snr_db=10.0)
    idle = _audio(render_idle(chain, seed=1, seconds=2.0))
    assert len(idle) == 2 * SAMPLE_RATE
    assert db(power(idle)) == pytest.approx(REFERENCE_LEVEL_DBFS - 10.0, abs=0.5)
    assert not _audio(render_idle(ImpairmentChain(), seed=1, seconds=0.5)).any()
