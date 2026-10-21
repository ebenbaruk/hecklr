"""A deterministic speech-like signal: voiced, formant-shaped syllables on a pitch contour.

It is not intelligible, but it has what the pipeline needs from speech: a fundamental and
harmonics in the telephone band, syllable-rate amplitude modulation, a duration that follows the
text length and speaking rate, and real pauses only at punctuation. Within a phrase the envelope
dips between syllables and words but never falls silent, so a VAD sees one continuous stretch of
speech, as it would with a human talker. Also used to build babble and TV noise.
"""

import re
from typing import Final

import numpy as np

from hecklr_voice.audio import SAMPLE_RATE, Audio

SYLLABLE_S: Final = 0.17
"""Mean syllable length at rate 1.0 (about 5.9 syllables per second, conversational English)."""
CLAUSE_PAUSE_S: Final = 0.08
SENTENCE_PAUSE_S: Final = 0.15
"""Kept below the harness's 250 ms speech hangover so one reply stays one speech segment."""

_VOWELS: Final = (
    (730.0, 1090.0, 2440.0),  # a
    (270.0, 2290.0, 3010.0),  # i
    (530.0, 1840.0, 2480.0),  # e
    (570.0, 840.0, 2410.0),  # o
    (300.0, 870.0, 2240.0),  # u
    (640.0, 1190.0, 2390.0),  # ʌ
    (440.0, 1020.0, 2240.0),  # ə
)
_FORMANT_WIDTH_HZ: Final = (90.0, 120.0, 160.0)
_FORMANT_GAIN: Final = (1.0, 0.6, 0.3)
_SYLLABLE_DIP: Final = 0.12
_WORD_DIP: Final = 0.06
_EDGE_S: Final = 0.005
_SENTENCE_ENDS: Final = frozenset(".!?")
_TOKEN = re.compile(r"[^\W_]+(?:'[^\W_]+)*|[.,;:!?]")


def phrases(text: str) -> list[tuple[list[str], float]]:
    """Split text into phrases of words, each with the pause that follows it (seconds)."""
    result: list[tuple[list[str], float]] = []
    words: list[str] = []
    for token in _TOKEN.findall(text):
        if token[0].isalnum():
            words.append(token)
            continue
        if words:
            result.append((words, SENTENCE_PAUSE_S if token in _SENTENCE_ENDS else CLAUSE_PAUSE_S))
            words = []
    if words:
        result.append((words, 0.0))
    return result


def syllable_count(word: str) -> int:
    """A rough syllable count: one per three letters, at least one."""
    return max(1, round(len(word) / 3))


def synthesize_speech(text: str, *, f0_hz: float, rate: float, seed: int) -> Audio:
    """Speech-like audio for ``text`` at ``SAMPLE_RATE``; identical inputs give identical audio.

    Args:
        text: What is "said"; only word lengths and punctuation shape the signal.
        f0_hz: Mean fundamental frequency of the voice.
        rate: Speaking-rate multiplier (2.0 is twice as fast).
        seed: Seeds syllable lengths, vowels and the pitch contour.
    """
    if f0_hz <= 0 or rate <= 0:
        raise ValueError(f"f0_hz and rate must be positive, got {f0_hz} and {rate}")
    generator = np.random.default_rng(seed)
    pieces: list[Audio] = []
    for words, pause_s in phrases(text):
        pieces.append(_phrase(words, f0_hz, rate, generator))
        pieces.append(np.zeros(round(pause_s * SAMPLE_RATE), dtype=np.float32))
    if not pieces:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(pieces).astype(np.float32)


def _phrase(words: list[str], f0_hz: float, rate: float, generator: np.random.Generator) -> Audio:
    lengths: list[int] = []
    dips: list[float] = []
    for word in words:
        for index in range(syllable_count(word)):
            seconds = SYLLABLE_S / rate * generator.uniform(0.8, 1.25)
            lengths.append(max(1, round(seconds * SAMPLE_RATE)))
            dips.append(_WORD_DIP if index == 0 else _SYLLABLE_DIP)
    syllable = np.repeat(np.arange(len(lengths)), lengths)
    n = len(syllable)
    position = np.concatenate([np.linspace(0.0, 1.0, length, endpoint=False) for length in lengths])
    envelope = np.asarray(dips)[syllable] + (1 - np.asarray(dips)[syllable]) * np.sin(
        np.pi * position
    )
    # Declination plus a small seeded wobble per syllable, like a falling statement contour.
    wobble = generator.uniform(-0.06, 0.06, len(lengths))[syllable]
    f0 = f0_hz * (1.08 - 0.16 * np.arange(n) / n) * (1 + wobble)
    phase = 2 * np.pi * np.cumsum(f0) / SAMPLE_RATE
    vowels = np.asarray(_VOWELS)[generator.integers(0, len(_VOWELS), len(lengths))]
    signal = np.zeros(n)
    for harmonic in range(1, int(3800 // (f0_hz * 1.2)) + 1):
        gain = _resonance(harmonic * f0, vowels[syllable]) / harmonic**0.5
        signal += gain * np.sin(harmonic * phase)
    signal += 0.02 * generator.standard_normal(n)
    signal *= envelope * _edges(n)
    peak = np.max(np.abs(signal))
    return (signal * (0.3 / peak if peak > 0 else 1.0)).astype(np.float32)


def _resonance(frequency: np.ndarray, formants: np.ndarray) -> np.ndarray:
    gain = np.zeros_like(frequency)
    for index in range(3):
        offset = (frequency - formants[:, index]) / _FORMANT_WIDTH_HZ[index]
        gain += _FORMANT_GAIN[index] * np.exp(-0.5 * offset**2)
    return gain + 0.02


def _edges(n: int) -> np.ndarray:
    ramp = min(n // 2, round(_EDGE_S * SAMPLE_RATE))
    window = np.ones(n)
    if ramp > 0:
        rise = 0.5 - 0.5 * np.cos(np.linspace(0, np.pi, ramp))
        window[:ramp] = rise
        window[n - ramp :] = rise[::-1]
    return window
