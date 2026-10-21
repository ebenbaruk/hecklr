"""Procedural background noise: seeded, license-free stand-ins for recorded environments.

Each generator imitates what matters about its environment for a voice agent: car interiors are
loud and low-frequency, streets swell and fade, construction is impulsive, a cafe is a bed of
overlapping voices (the hardest case for a VAD), a TV is one voice plus music, and a crying child
is loud, high-pitched and intermittent. Recorded libraries can replace these later, once their
licenses are verified (spec, "Outbound path"); the ids stay the same.
"""

from collections.abc import Callable
from typing import Final

import numpy as np
from scipy.signal import butter, sosfilt

from hecklr_voice.audio import SAMPLE_RATE, Audio
from hecklr_voice.synth import synthesize_speech

_BABBLE_TEXT: Final = (
    "well I told him that we would be there by seven but the traffic was terrible so "
    "maybe next week could work if the weather holds and everyone agrees on the place"
)


def _unit_rms(signal: np.ndarray) -> Audio:
    rms = float(np.sqrt(np.mean(np.square(signal))))
    return (signal / rms if rms > 0 else signal).astype(np.float32)


def _lowpass(signal: np.ndarray, cutoff_hz: float) -> np.ndarray:
    sos = butter(4, cutoff_hz, btype="lowpass", fs=SAMPLE_RATE, output="sos")
    return np.asarray(sosfilt(sos, signal))


def _bandpass(signal: np.ndarray, low_hz: float, high_hz: float) -> np.ndarray:
    sos = butter(4, (low_hz, high_hz), btype="bandpass", fs=SAMPLE_RATE, output="sos")
    return np.asarray(sosfilt(sos, signal))


def _pink(n: int, generator: np.random.Generator) -> np.ndarray:
    spectrum = np.fft.rfft(generator.standard_normal(n))
    freqs = np.fft.rfftfreq(n, 1 / SAMPLE_RATE)
    spectrum[1:] /= np.sqrt(freqs[1:])
    spectrum[0] = 0
    return np.asarray(np.fft.irfft(spectrum, n))


def _brown(n: int, generator: np.random.Generator) -> np.ndarray:
    walk = np.cumsum(generator.standard_normal(n))
    # Remove the random walk's drift so the noise stays centered over long calls.
    return np.asarray(walk - _lowpass(walk, 5.0), dtype=np.float64)


def _slow_envelope(n: int, generator: np.random.Generator, period_s: float) -> np.ndarray:
    knots = max(2, int(n / SAMPLE_RATE / period_s) + 2)
    values = generator.uniform(0.2, 1.0, knots)
    return np.interp(np.arange(n), np.linspace(0, n, knots), values)


def _voice(n: int, generator: np.random.Generator, f0_range: tuple[float, float]) -> np.ndarray:
    words = generator.choice(_BABBLE_TEXT.split(), size=max(4, n // 2500))
    text = " ".join(str(word) for word in words)
    speech = synthesize_speech(
        text,
        f0_hz=float(generator.uniform(*f0_range)),
        rate=float(generator.uniform(0.9, 1.2)),
        seed=int(generator.integers(2**32)),
    )
    speech = np.resize(speech, n)
    return np.roll(speech, int(generator.integers(n))) if n else speech


def car_interior(n: int, generator: np.random.Generator) -> Audio:
    """Low-passed brown road noise plus a wandering engine hum."""
    road = _lowpass(_brown(n, generator), 400.0)
    t = np.arange(n) / SAMPLE_RATE
    rpm_hz = 30 + 5 * np.sin(2 * np.pi * t / generator.uniform(4, 9))
    hum = np.sin(2 * np.pi * np.cumsum(rpm_hz) / SAMPLE_RATE)
    return _unit_rms(_unit_rms(road) + 0.3 * hum)


def street(n: int, generator: np.random.Generator) -> Audio:
    """Pink traffic bed with passing-vehicle swells."""
    bed = _lowpass(_pink(n, generator), 3000.0)
    return _unit_rms(bed * _slow_envelope(n, generator, 2.5))


def construction(n: int, generator: np.random.Generator) -> Audio:
    """Hammer impacts (decaying broadband bursts) and drill runs over a low rumble."""
    signal = 0.2 * _unit_rms(_lowpass(_brown(n, generator), 300.0))
    hit_length = int(0.12 * SAMPLE_RATE)
    decay = np.exp(-np.arange(hit_length) / (0.02 * SAMPLE_RATE))
    for start in np.flatnonzero(generator.random(n) < 3.0 / SAMPLE_RATE):
        end = min(n, int(start) + hit_length)
        signal[start:end] += 4 * generator.standard_normal(end - start) * decay[: end - start]
    drill = np.sign(np.sin(2 * np.pi * 90 * np.arange(n) / SAMPLE_RATE))
    drilling = _slow_envelope(n, generator, 1.5) > 0.7
    signal += 0.8 * drill * generator.standard_normal(n) * drilling
    return _unit_rms(signal)


def cafe(n: int, generator: np.random.Generator) -> Audio:
    """Babble of six overlapping synthetic talkers, plus cutlery clicks and a pink bed."""
    babble = np.sum([_voice(n, generator, (90.0, 240.0)) for _ in range(6)], axis=0)
    clicks = np.zeros(n)
    for start in np.flatnonzero(generator.random(n) < 1.5 / SAMPLE_RATE):
        end = min(n, int(start) + 40)
        clicks[start:end] = generator.standard_normal(end - start)
    return _unit_rms(_unit_rms(babble) + 0.3 * _unit_rms(_pink(n, generator)) + 0.5 * clicks)


def tv(n: int, generator: np.random.Generator) -> Audio:
    """One presenter's voice alternating with a chord-based music bed."""
    t = np.arange(n) / SAMPLE_RATE
    root = generator.choice([196.0, 220.0, 247.0, 262.0])
    music = np.sum(
        [np.sin(2 * np.pi * root * ratio * t) for ratio in (1.0, 1.25, 1.5, 2.0)], axis=0
    )
    talking = _slow_envelope(n, generator, 3.0) > 0.55
    voice = _unit_rms(_voice(n, generator, (100.0, 200.0)))
    mixed = np.where(talking, voice, 0.5 * _unit_rms(music))
    return _unit_rms(_bandpass(mixed, 150.0, 5000.0))


def crying_child(n: int, generator: np.random.Generator) -> Audio:
    """Loud, high-pitched cries with vibrato, in bursts separated by breaths."""
    t = np.arange(n) / SAMPLE_RATE
    f0 = generator.uniform(400, 550) * (1 + 0.04 * np.sin(2 * np.pi * 6 * t))
    phase = 2 * np.pi * np.cumsum(f0) / SAMPLE_RATE
    cry = np.sum([np.sin(k * phase) / k for k in range(1, 6)], axis=0)
    bursts = np.zeros(n, dtype=bool)
    position = 0
    while position < n:
        length = int(generator.uniform(0.5, 1.5) * SAMPLE_RATE)
        bursts[position : position + length] = True
        position += length + int(generator.uniform(0.3, 0.8) * SAMPLE_RATE)
    smooth = np.convolve(bursts.astype(float), np.hanning(800) / np.hanning(800).sum(), "same")
    return _unit_rms(cry * smooth + 0.05 * generator.standard_normal(n))


NoiseGenerator = Callable[[int, np.random.Generator], Audio]

NOISES: Final[dict[str, NoiseGenerator]] = {
    "car_interior": car_interior,
    "street": street,
    "construction": construction,
    "cafe": cafe,
    "babble": cafe,
    "tv": tv,
    "crying_child": crying_child,
}
"""Noise environment ids accepted in ``persona.voice.environment``."""


def generate_noise(environment: str, n: int, seed: int) -> Audio:
    """``n`` samples of ``environment`` noise at unit RMS, identical for identical inputs."""
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    return NOISES[environment](n, np.random.default_rng(seed))
