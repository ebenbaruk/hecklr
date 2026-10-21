"""Network impairments on the 20 ms frame stream: random and bursty loss, concealment, jitter.

Real VoIP loss comes in bursts, so a persona's ``packet_loss`` drives a Gilbert-Elliott channel
(a good and a bad state; every frame sent in the bad state is lost) whose long-run loss rate is
``packet_loss`` and whose mean burst is :data:`DEFAULT_MEAN_BURST_FRAMES`. Lost frames are then
concealed the way a receiver would, so the agent hears what a real lossy line delivers.
"""

from typing import Final, Literal

import numpy as np

from hecklr_voice.audio import FRAME_MS, FRAME_SAMPLES, Audio

DEFAULT_MEAN_BURST_FRAMES: Final = 2.0
"""Mean loss burst: 40 ms, typical of congested access links."""

Concealment = Literal["repeat", "zero"]


def gilbert_elliott_loss(
    frames: int, rate: float, mean_burst: float, generator: np.random.Generator
) -> np.ndarray:
    """Bursty loss with long-run loss ``rate`` and mean burst length ``mean_burst`` frames.

    Transition probabilities: bad -> good is ``r = 1 / mean_burst``, good -> bad is ``p``, chosen
    so the stationary share of bad frames, ``p / (p + r)``, equals ``rate``. With
    ``mean_burst = 1 / (1 - rate)`` both states lose the next frame with probability ``rate``,
    which is independent (Bernoulli) loss.
    """
    _check_rate(rate)
    if mean_burst < 1.0:
        raise ValueError(f"mean_burst must be >= 1 frame, got {mean_burst}")
    if rate == 0.0 or frames == 0:
        return np.zeros(frames, dtype=bool)
    to_good = 1.0 / mean_burst
    to_bad = rate * to_good / (1.0 - rate)
    if to_bad > 1.0:
        raise ValueError(f"loss rate {rate} is unreachable with mean burst {mean_burst} frames")
    draws = generator.random(frames)
    lost = np.empty(frames, dtype=bool)
    bad = bool(generator.random() < rate)
    for index in range(frames):
        lost[index] = bad
        bad = draws[index] >= to_good if bad else draws[index] < to_bad
    return lost


def conceal(audio: Audio, lost: np.ndarray, method: Concealment = "repeat") -> Audio:
    """Replace the lost frames of ``audio`` as a receiver's packet-loss concealment would.

    ``repeat`` replays the last received frame, halving it for every further consecutive loss
    (a receiver fades out rather than buzzing); ``zero`` inserts silence.
    """
    frames = len(audio) // FRAME_SAMPLES
    if len(lost) < frames:
        raise ValueError(f"loss mask covers {len(lost)} frames, audio has {frames}")
    out = audio.copy()
    last = np.zeros(FRAME_SAMPLES, dtype=np.float32)
    gain = 1.0
    for index in range(frames):
        frame = slice(index * FRAME_SAMPLES, (index + 1) * FRAME_SAMPLES)
        if not lost[index]:
            last, gain = audio[frame], 1.0
            continue
        gain *= 0.5
        out[frame] = last * gain if method == "repeat" else 0.0
    return out


class SendJitter:
    """Per-frame extra send delay, uniform in [0, ``jitter_ms``], that never reorders frames.

    A frame may not leave before the one ahead of it, so each delay is at least the previous
    delay minus one frame period.
    """

    def __init__(self, jitter_ms: int, generator: np.random.Generator) -> None:
        if jitter_ms < 0:
            raise ValueError(f"jitter_ms must be >= 0, got {jitter_ms}")
        self._jitter_ms = jitter_ms
        self._generator = generator
        self._previous = 0.0

    def next_delay_ms(self) -> float:
        """The delay of the next frame, in milliseconds."""
        if self._jitter_ms == 0:
            return 0.0
        drawn = float(self._generator.uniform(0, self._jitter_ms))
        self._previous = max(drawn, self._previous - FRAME_MS)
        return self._previous


def _check_rate(rate: float) -> None:
    if not 0.0 <= rate < 1.0:
        raise ValueError(f"loss rate must be in [0, 1), got {rate}")
