"""The caller's impairment chain, applied in the spec's order as one seeded, deterministic job.

1. Speaking-rate and pitch jitter: a per-segment speed perturbation (tempo and pitch move
   together, as in Kaldi-style speed perturbation; a persona's base rate and pitch are the TTS's).
2. Background noise mixed at the persona's SNR against the active speech level.
3. Room reverb from a synthetic exponential-decay impulse response.
4. Telephone band-pass and codec.
5. Network loss on 20 ms frames, with packet-loss concealment.

Jitter of the send times (the last network impairment) is applied by the frame sender.
:func:`render_clip` and :func:`render_idle` are pool jobs: run them through ``run_dsp``.
"""

import math
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Final, Literal, Self

import numpy as np
from scipy.signal import fftconvolve, resample_poly

from hecklr_schemas import LineCodec, VoiceProfile
from hecklr_voice.audio import (
    FRAME_SAMPLES,
    REFERENCE_LEVEL_DBFS,
    SAMPLE_RATE,
    Audio,
    active_power,
    normalize_active_level,
    pad_to_frames,
    pcm_bytes,
    speech_edges,
    stream_rng,
    stream_seed,
)
from hecklr_voice.codec import apply_line, require_line
from hecklr_voice.errors import VoiceConfigError
from hecklr_voice.network import (
    DEFAULT_MEAN_BURST_FRAMES,
    Concealment,
    conceal,
    gilbert_elliott_loss,
)
from hecklr_voice.noise import NOISES, generate_noise

DEFAULT_SPEED_JITTER: Final = 0.03
"""Each spoken segment is sped up or slowed down by up to 3%."""

ROOMS: Final[dict[str, float]] = {
    "small_room": 0.3,
    "office": 0.5,
    "living_room": 0.6,
    "bathroom": 1.0,
    "hall": 1.6,
}
"""Reverb ids accepted in ``persona.voice.reverb``, with their RT60 in seconds."""

SegmentContent = Literal["speech", "filler", "backchannel", "pause"]


@dataclass(frozen=True, slots=True)
class ImpairmentChain:
    """The impairments of one caller, built from a persona's ``VoiceProfile``."""

    speed_jitter: float = 0.0
    environment: str | None = None
    snr_db: float | None = None
    reverb: str | None = None
    line: LineCodec = LineCodec.WIDEBAND
    packet_loss: float = 0.0
    mean_burst_frames: float = DEFAULT_MEAN_BURST_FRAMES
    concealment: Concealment = "repeat"

    def __post_init__(self) -> None:
        if self.environment is not None and self.environment not in NOISES:
            known = ", ".join(sorted(NOISES))
            raise VoiceConfigError(f"unknown environment {self.environment!r}; known: {known}")
        if self.environment is not None and self.snr_db is None:
            raise VoiceConfigError("a noise environment needs snr_db")
        if self.reverb is not None and self.reverb not in ROOMS:
            known = ", ".join(sorted(ROOMS))
            raise VoiceConfigError(f"unknown reverb {self.reverb!r}; known: {known}")
        if not 0.0 <= self.speed_jitter < 0.5:
            raise VoiceConfigError(f"speed_jitter must be in [0, 0.5), got {self.speed_jitter}")
        require_line(self.line)

    @classmethod
    def for_voice(cls, profile: VoiceProfile) -> Self:
        """The chain a persona's voice profile asks for."""
        return cls(
            speed_jitter=DEFAULT_SPEED_JITTER,
            environment=profile.environment,
            snr_db=profile.snr_db,
            reverb=profile.reverb,
            line=profile.line,
            packet_loss=profile.packet_loss,
        )


@dataclass(frozen=True, slots=True)
class Segment:
    """One piece of a caller clip: spoken audio, or a pause (zeros) of a planned length."""

    content: SegmentContent
    audio: Audio

    @classmethod
    def pause(cls, ms: int) -> Self:
        """A silent pause of ``ms`` milliseconds."""
        return cls("pause", np.zeros(ms * SAMPLE_RATE // 1000, dtype=np.float32))


@dataclass(frozen=True, slots=True)
class Span:
    """Where a spoken segment sounds within a clip, in samples (end exclusive)."""

    content: Literal["speech", "filler", "backchannel"]
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class RenderedClip:
    """A caller clip ready to send: whole 20 ms frames of 16-bit PCM and its spoken spans."""

    pcm: bytes
    spans: tuple[Span, ...]
    lost_frames: int

    @property
    def samples(self) -> int:
        """Length in samples (a multiple of the frame size)."""
        return len(self.pcm) // 2


def speed_perturb(audio: Audio, factor: float) -> Audio:
    """Play ``audio`` ``factor`` times faster (shorter and higher), by polyphase resampling."""
    if factor == 1.0 or len(audio) == 0:
        return audio
    ratio = Fraction(1 / factor).limit_denominator(200)
    out = resample_poly(audio.astype(np.float64), ratio.numerator, ratio.denominator)
    return np.asarray(out, dtype=np.float32)


def room_impulse_response(room: str, seed: int) -> Audio:
    """A synthetic impulse response: the direct path plus an exponentially decaying noise tail."""
    rt60 = ROOMS[room]
    n = int(min(rt60, 1.0) * SAMPLE_RATE)
    t = np.arange(n) / SAMPLE_RATE
    tail = np.random.default_rng(seed).standard_normal(n) * np.exp(-6.91 * t / rt60)
    tail[0] = 0.0
    # Equal direct and reverberant energy: clearly roomy, still intelligible.
    tail *= 1.0 / np.sqrt(np.sum(np.square(tail)))
    tail[0] = 1.0
    return tail.astype(np.float32)


def mix_at_snr(speech: Audio, noise: Audio, snr_db: float) -> Audio:
    """Add ``noise`` scaled so active speech power over noise power is ``snr_db``."""
    noise_power = float(np.mean(np.square(noise, dtype=np.float64)))
    speech_power = active_power(speech)
    if noise_power == 0.0 or speech_power == 0.0:
        return speech
    gain = math.sqrt(speech_power / (noise_power * 10 ** (snr_db / 10)))
    return (speech + np.float32(gain) * noise).astype(np.float32)


def render_clip(segments: tuple[Segment, ...], chain: ImpairmentChain, seed: int) -> RenderedClip:
    """Pool job: assemble segments and run the whole chain; deterministic in ``seed``."""
    pieces: list[Audio] = []
    spans: list[Span] = []
    position = 0
    for index, segment in enumerate(segments):
        audio = segment.audio
        if segment.content != "pause" and chain.speed_jitter > 0:
            limit = chain.speed_jitter
            jitter = stream_rng(seed, "speed", index).uniform(-limit, limit)
            audio = speed_perturb(audio, 1.0 + jitter)
        if segment.content != "pause" and (edges := speech_edges(audio)) is not None:
            spans.append(Span(segment.content, position + edges[0], position + edges[1]))
        pieces.append(audio)
        position += len(audio)
    clean = pad_to_frames(np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32))
    audio, lost = _impair(normalize_active_level(clean), chain, seed)
    return RenderedClip(pcm=pcm_bytes(audio), spans=tuple(spans), lost_frames=lost)


def render_idle(chain: ImpairmentChain, seed: int, seconds: float) -> bytes:
    """Pool job: what the line carries while the caller is silent (noise only, same chain)."""
    n = pad_to_frames(np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)).size
    if chain.environment is None or chain.snr_db is None:
        audio = np.zeros(n, dtype=np.float32)
    else:
        level = 10 ** ((REFERENCE_LEVEL_DBFS - chain.snr_db) / 20)
        audio = level * generate_noise(chain.environment, n, stream_seed(seed, "idle-noise"))
    # The noise is already at its absolute level; the rest of the chain must not add more.
    impaired, _ = _impair(audio, replace(chain, environment=None, snr_db=None), seed)
    return pcm_bytes(impaired)


def _impair(audio: Audio, chain: ImpairmentChain, seed: int) -> tuple[Audio, int]:
    """Steps 2-5 of the chain on frame-aligned audio; returns the audio and lost-frame count."""
    if chain.environment is not None and chain.snr_db is not None:
        noise = generate_noise(chain.environment, len(audio), stream_seed(seed, "noise"))
        audio = mix_at_snr(audio, noise, chain.snr_db)
    if chain.reverb is not None:
        response = room_impulse_response(chain.reverb, stream_seed(seed, "room", chain.reverb))
        audio = fftconvolve(audio, response)[: len(audio)].astype(np.float32)
    audio = apply_line(audio, chain.line)
    if chain.packet_loss == 0.0:
        return audio, 0
    frames = len(audio) // FRAME_SAMPLES
    generator = stream_rng(seed, "loss")
    lost = gilbert_elliott_loss(frames, chain.packet_loss, chain.mean_burst_frames, generator)
    return conceal(audio, lost, chain.concealment), int(lost.sum())
