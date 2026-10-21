"""Events: the timestamped record of everything that happened in a trial.

Each event kind has its own payload model, discriminated on ``kind`` (ADR 0002), so graders and
reports read typed fields instead of guessing at dict keys.
"""

import base64
from typing import Annotated, Literal, Self

from pydantic import (
    BeforeValidator,
    Field,
    JsonValue,
    PlainSerializer,
    StringConstraints,
    model_validator,
)

from hecklr_schemas.common import Actor, FrozenModel, NonNegativeMs
from hecklr_schemas.ids import EventIdField, TrialIdField
from hecklr_schemas.world import FaultKind

Party = Literal["user", "agent"]

EmailAddress = Annotated[str, StringConstraints(pattern=r"^[^@\s<>]+@[^@\s<>]+$")]
"""A bare address. Deliberately loose: reserved test domains such as ``acme.test`` must pass."""


def _decode_base64(value: object) -> object:
    return base64.b64decode(value, validate=True) if isinstance(value, str) else value


def _encode_base64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


Base64Data = Annotated[
    bytes,
    BeforeValidator(_decode_base64),
    PlainSerializer(_encode_base64, return_type=str, when_used="json"),
]
"""Raw bytes in Python, a base64 string in JSON."""


class EmailAttachment(FrozenModel):
    """A file attached to an email."""

    filename: str
    content_type: str = "application/octet-stream"
    data: Base64Data


class EmailMessage(FrozenModel):
    """One email, as delivered to or received from an email agent."""

    message_id: str
    """RFC 5322 ``Message-ID``, including angle brackets."""
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    subject: str
    body: str
    from_addr: EmailAddress
    to_addrs: Annotated[list[EmailAddress], Field(min_length=1)]
    attachments: list[EmailAttachment] = Field(default_factory=list)


class UtterancePayload(FrozenModel):
    """A complete message or spoken turn (for voice, the ASR transcript or the TTS text)."""

    kind: Literal["utterance"] = "utterance"
    text: str
    turn: Annotated[int, Field(ge=0)]
    """Index of the turn within the trial, counting both parties."""
    interrupted: bool = False
    """The turn was cut off by the other party."""
    asr_confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = None


class AudioSpanPayload(FrozenModel):
    """A span of audio on one side of a call; ``Event.t_ms`` equals ``start_ms``."""

    kind: Literal["audio_span"] = "audio_span"
    start_ms: NonNegativeMs
    end_ms: NonNegativeMs
    content: Literal["speech", "backchannel", "filler", "noise"] = "speech"
    start_sample: Annotated[int, Field(ge=0)] | None = None
    """Sample-accurate start on the recording, for waveform markers."""
    sample_rate: Annotated[int, Field(gt=0)] | None = None
    recording: str | None = None
    """Path of the WAV file, relative to the run directory."""

    @model_validator(mode="after")
    def _ends_after_start(self) -> Self:
        if self.end_ms < self.start_ms:
            raise ValueError(f"end_ms {self.end_ms} precedes start_ms {self.start_ms}")
        return self


class ToolCallPayload(FrozenModel):
    """The agent called a world tool."""

    kind: Literal["tool_call"] = "tool_call"
    call_id: str
    tool: str
    args: dict[str, JsonValue] = Field(default_factory=dict)
    source: Literal["sandbox", "observed"] = "sandbox"
    """``observed``: read from the agent's OpenTelemetry spans rather than the sandbox."""


class ToolResultPayload(FrozenModel):
    """What a tool call returned (or how it failed)."""

    kind: Literal["tool_result"] = "tool_result"
    call_id: str
    tool: str
    result: JsonValue = None
    error: str | None = None
    latency_ms: NonNegativeMs
    fault: FaultKind | None = None
    """The injected fault that shaped this result, if any."""

    @model_validator(mode="after")
    def _result_xor_error(self) -> Self:
        if self.error is not None and self.result is not None:
            raise ValueError("a tool result carries either result or error, not both")
        return self

    @property
    def ok(self) -> bool:
        """Whether the call succeeded."""
        return self.error is None


class InterruptPayload(FrozenModel):
    """``Event.actor`` started speaking over ``target``."""

    kind: Literal["interrupt"] = "interrupt"
    target: Party
    style: Literal["barge_in", "backchannel"] = "barge_in"
    """A backchannel ("mm-hm") is overlap the agent must not treat as an interruption."""


class SilencePayload(FrozenModel):
    """A gap with no speech from either party."""

    kind: Literal["silence"] = "silence"
    start_ms: NonNegativeMs
    end_ms: NonNegativeMs
    attributed_to: Party | None = None
    """Whose turn it was to speak; ``None`` if ambiguous."""

    @model_validator(mode="after")
    def _ends_after_start(self) -> Self:
        if self.end_ms < self.start_ms:
            raise ValueError(f"end_ms {self.end_ms} precedes start_ms {self.start_ms}")
        return self


class EmailPayload(FrozenModel):
    """An email sent by ``Event.actor``."""

    kind: Literal["email"] = "email"
    message: EmailMessage


class HandoffPayload(FrozenModel):
    """The conversation was handed to a human."""

    kind: Literal["handoff"] = "handoff"
    reason: str | None = None
    target: str | None = None


class ErrorPayload(FrozenModel):
    """Something failed during the trial."""

    kind: Literal["error"] = "error"
    source: Literal["agent", "harness", "simulator", "world"]
    message: str
    retryable: bool = False


EventPayload = Annotated[
    UtterancePayload
    | AudioSpanPayload
    | ToolCallPayload
    | ToolResultPayload
    | InterruptPayload
    | SilencePayload
    | EmailPayload
    | HandoffPayload
    | ErrorPayload,
    Field(discriminator="kind"),
]

EventKind = Literal[
    "utterance",
    "audio_span",
    "tool_call",
    "tool_result",
    "interrupt",
    "silence",
    "email",
    "handoff",
    "error",
]


class Event(FrozenModel):
    """A timestamped item in a trial."""

    id: EventIdField
    trial_id: TrialIdField
    seq: Annotated[int, Field(ge=0)]
    """Position in the trial's event log; orders events that share a millisecond."""
    t_ms: NonNegativeMs
    """Harness monotonic clock, milliseconds since the trial started."""
    actor: Actor
    payload: EventPayload

    @property
    def kind(self) -> EventKind:
        """The payload's kind."""
        return self.payload.kind


class EventRef(FrozenModel):
    """A pointer to one event, used as grader evidence."""

    trial_id: TrialIdField
    event_id: EventIdField
