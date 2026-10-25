"""Configuration of the ``websocket`` voice adapter.

The frozen ``hecklr_schemas.VoiceAdapterConfig`` has no WebSocket member, so this model lives
here until RFC 0010a adds it to the schema; ``make_voice_target`` accepts it next to the
schema's voice configurations.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from hecklr_adapters.voice.audio import MULAW_RATE, SAMPLE_RATE
from hecklr_schemas.common import FrozenModel
from hecklr_schemas.config import EnvVarName

WireProtocol = Literal["pcm", "twilio", "pipecat"]


class WebSocketVoiceAdapterConfig(FrozenModel):
    """A voice agent reached over one WebSocket per call.

    ``protocol`` picks the message format:

    - ``pcm``: binary messages of raw 16-bit little-endian mono PCM at ``sample_rate`` both
      ways; text messages are JSON control messages (``{"type": "clear"}`` from the agent).
    - ``twilio``: Twilio Media Streams, bidirectional: JSON ``connected`` / ``start`` /
      ``media`` / ``stop`` with base64 G.711 mu-law at 8 kHz; the agent's ``mark`` and
      ``clear`` work as on Twilio. Most telephony-facing agent stacks speak it.
    - ``pipecat``: Pipecat's ``ProtobufFrameSerializer`` (its default WebSocket format):
      binary ``Frame`` messages carrying ``AudioRawFrame`` audio and ``InterruptionFrame``.
    """

    type: Literal["websocket"]
    url: Annotated[str, Field(pattern=r"^wss?://")]
    protocol: WireProtocol = "pcm"
    sample_rate: Annotated[int, Field(ge=8_000, le=48_000)] | None = None
    """Audio rate on the wire; 16 kHz by default, always 8 kHz for ``twilio``."""
    headers: dict[str, str] = Field(default_factory=dict)
    headers_env: dict[str, EnvVarName] = Field(default_factory=dict)
    """Headers whose value is read from an environment variable when the adapter starts: header
    name -> variable name, e.g. ``{Authorization: AGENT_AUTH_HEADER}`` (RFC 0023)."""
    connect_timeout_s: Annotated[float, Field(gt=0)] = 10.0

    @model_validator(mode="after")
    def _twilio_is_narrowband(self) -> Self:
        if self.protocol == "twilio" and self.sample_rate not in (None, MULAW_RATE):
            raise ValueError("the twilio protocol carries 8 kHz mu-law; drop sample_rate")
        return self

    @property
    def wire_rate(self) -> int:
        """The sample rate audio travels at."""
        if self.protocol == "twilio":
            return MULAW_RATE
        return self.sample_rate or SAMPLE_RATE
