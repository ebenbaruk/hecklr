"""Personas: who the simulated customer is, how they behave and how they sound."""

from enum import StrEnum
from typing import Annotated, Self

from pydantic import Field, model_validator

from hecklr_schemas.common import FrozenModel, Identifier, Probability, SchemaVersion

PositiveSeconds = Annotated[float, Field(ge=0.0)]


class Traits(FrozenModel):
    """Personality dials in [0, 1] that shape the simulator's language and patience."""

    patience: Probability = 0.5
    verbosity: Probability = 0.5
    cooperativeness: Probability = 0.5


class Behaviors(FrozenModel):
    """Turn-taking behaviors; voice uses all of them, chat and email only the textual ones."""

    interrupt_prob: Probability = 0.0
    """Chance per agent turn that the caller barges in."""
    backchannel_prob: Probability = 0.0
    """Chance per long agent turn of an "mm-hm" that must not stop the agent."""
    silence_prob: Probability = 0.0
    """Chance per caller turn of going quiet for 5-10 s to test re-engagement."""
    mid_utterance_pause_s: tuple[PositiveSeconds, PositiveSeconds] | None = None
    """Range of a hesitation inserted mid-utterance, to test premature endpointing."""
    response_gap_ms: tuple[Annotated[int, Field(ge=0)], Annotated[int, Field(ge=0)]] = (200, 900)
    """Range of the pause between agent end-of-speech and the caller's reply."""
    fillers: bool = False
    """Whether utterances include fillers such as "uh, hold on"."""

    @model_validator(mode="after")
    def _ranges_are_ordered(self) -> Self:
        for name in ("mid_utterance_pause_s", "response_gap_ms"):
            bounds = getattr(self, name)
            if bounds is not None and bounds[0] > bounds[1]:
                raise ValueError(f"{name}: lower bound {bounds[0]} exceeds upper {bounds[1]}")
        return self


class TtsConfig(FrozenModel):
    """Text-to-speech settings; output is cached by (text, voice, settings) hash."""

    provider: Identifier
    """``kokoro``, ``piper``, ``cartesia``, ``elevenlabs``, ``openai``, ..."""
    voice: str
    rate: Annotated[float, Field(gt=0.25, le=4.0)] = 1.0
    pitch_semitones: Annotated[float, Field(ge=-12.0, le=12.0)] = 0.0


class LineCodec(StrEnum):
    """Telephone band-pass and codec applied to the caller's audio."""

    WIDEBAND = "wideband"
    """No telephone impairment: 16 kHz PCM as WebRTC delivers it."""
    G711_MULAW_8K = "g711_mulaw_8k"
    AMR_NB = "amr_nb"
    OPUS_LOW = "opus_low"


class VoiceProfile(FrozenModel):
    """How the caller sounds and the line they call from; the impairment chain's inputs."""

    tts: TtsConfig
    accent: str | None = None
    environment: Identifier | None = None
    """Background noise id, e.g. ``car_interior``, ``street``, ``cafe``; ``None`` is silence."""
    snr_db: float | None = None
    """Speech-to-noise ratio for the background mix; ``None`` means clean."""
    reverb: Identifier | None = None
    """Room impulse response id."""
    line: LineCodec = LineCodec.WIDEBAND
    packet_loss: Annotated[float, Field(ge=0.0, lt=1.0)] = 0.0
    jitter_ms: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _noise_has_level(self) -> Self:
        if self.environment is not None and self.snr_db is None:
            raise ValueError("a noise environment needs snr_db")
        return self


class Persona(FrozenModel):
    """A simulated customer, reusable across scenarios."""

    schema_version: SchemaVersion = 1
    id: Identifier
    description: str
    language: Annotated[str, Field(pattern=r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")] = "en-US"
    """BCP 47 language tag."""
    traits: Traits = Traits()
    behaviors: Behaviors = Behaviors()
    voice: VoiceProfile | None = None
    """Required to run the persona on the voice channel."""
    adversarial: Identifier | None = None
    """Attack strategy id, e.g. ``refund_social_engineering``; ``None`` for a normal customer."""
