"""Message formats of the ``websocket`` voice adapter: raw PCM, Twilio Media Streams, Pipecat.

Each wire turns caller audio (at its own rate) into messages and agent messages into
:data:`Inbound` events. They hold no socket and do no I/O, so they are tested message by
message. Malformed agent messages raise ``AgentError``: the agent broke the protocol.
"""

import base64
import binascii
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final, Protocol

import numpy as np

from hecklr_adapters.context import session_fields
from hecklr_adapters.errors import AgentError
from hecklr_adapters.voice.audio import FRAME_MS, MULAW_RATE, Pcm16, mulaw_decode, mulaw_encode
from hecklr_schemas import SessionContext

Message = str | bytes


@dataclass(frozen=True, slots=True)
class AgentAudio:
    """Agent speech, mono, at ``sample_rate``."""

    samples: Pcm16
    sample_rate: int


@dataclass(frozen=True, slots=True)
class AgentClear:
    """The agent stopped talking: drop what it sent but has not been played yet."""


@dataclass(frozen=True, slots=True)
class AgentMark:
    """The agent asks to be told when the audio it has sent so far has played."""

    name: str


Inbound = AgentAudio | AgentClear | AgentMark | None
"""What one agent message means; ``None`` for messages that carry nothing for the call."""


class Wire(Protocol):
    """One WebSocket voice message format."""

    def opening(self) -> list[Message]:
        """Messages that start the call."""
        ...

    def audio(self, samples: Pcm16) -> Message:
        """One chunk of caller audio, already at the wire's rate."""
        ...

    def closing(self) -> list[Message]:
        """Messages that end the call before the socket closes."""
        ...

    def read(self, message: Message) -> Inbound:
        """Interpret one agent message."""
        ...

    def mark_played(self, name: str) -> Message | None:
        """The reply to a mark whose audio has played or was cleared, if the format has one."""
        ...


def _json(message: Message) -> dict[str, object]:
    if not isinstance(message, str):
        raise AgentError("agent sent a binary message where JSON text was expected")
    try:
        data = json.loads(message)
    except (ValueError, RecursionError) as exc:  # as in ``parse_json`` (issue #112)
        raise AgentError(f"agent sent malformed JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentError("agent sent a JSON message that is not an object")
    return data


def _pcm(data: bytes) -> Pcm16:
    if len(data) % 2:
        raise AgentError(f"agent sent {len(data)} bytes of 16-bit audio (an odd count)")
    return np.frombuffer(data, dtype="<i2").astype(np.int16)


# --- pcm -------------------------------------------------------------------------------------


class PcmWire:
    """Raw PCM16 binary frames, JSON text control messages."""

    def __init__(self, ctx: SessionContext, session_id: str, sample_rate: int) -> None:
        self._start: dict[str, object] = {
            "type": "start",
            "session_id": session_id,
            **session_fields(ctx),
            "sample_rate": sample_rate,
            "encoding": "pcm_s16le",
            "channels": 1,
        }
        self._rate = sample_rate

    def opening(self) -> list[Message]:
        """A ``start`` message announcing the format, the trial and its world (ADR 0007e)."""
        return [json.dumps(self._start)]

    def audio(self, samples: Pcm16) -> Message:
        """The samples as little-endian bytes."""
        return samples.astype("<i2").tobytes()

    def closing(self) -> list[Message]:
        """A ``stop`` message."""
        return [json.dumps({"type": "stop"})]

    def read(self, message: Message) -> Inbound:
        """Binary is audio; ``{"type": "clear"}`` is a barge-in stop; other JSON is ignored."""
        if isinstance(message, bytes):
            return AgentAudio(_pcm(message), self._rate)
        return AgentClear() if _json(message).get("type") == "clear" else None

    def mark_played(self, name: str) -> Message | None:
        """The pcm protocol has no marks."""
        return None


# --- twilio ----------------------------------------------------------------------------------


class TwilioWire:
    """Twilio Media Streams, as Twilio's side of a bidirectional stream speaks it."""

    def __init__(self, ctx: SessionContext, session_id: str) -> None:
        self._stream_sid = f"MZ{session_id}"
        self._call_sid = f"CA{session_id}"
        self._parameters = session_fields(ctx)
        self._sequence = 0
        self._chunk = 0

    def opening(self) -> list[Message]:
        """``connected`` then ``start``, the trial id and world URLs as custom parameters."""
        connected = {"event": "connected", "protocol": "Call", "version": "1.0.0"}
        start = {
            "event": "start",
            "sequenceNumber": self._next_sequence(),
            "start": {
                "accountSid": "AChecklr",
                "streamSid": self._stream_sid,
                "callSid": self._call_sid,
                "tracks": ["inbound"],
                "mediaFormat": {
                    "encoding": "audio/x-mulaw",
                    "sampleRate": MULAW_RATE,
                    "channels": 1,
                },
                "customParameters": self._parameters,
            },
            "streamSid": self._stream_sid,
        }
        return [json.dumps(connected), json.dumps(start)]

    def audio(self, samples: Pcm16) -> Message:
        """A ``media`` message on the inbound track."""
        self._chunk += 1
        return json.dumps(
            {
                "event": "media",
                "sequenceNumber": self._next_sequence(),
                "media": {
                    "track": "inbound",
                    "chunk": str(self._chunk),
                    "timestamp": str((self._chunk - 1) * FRAME_MS),
                    "payload": base64.b64encode(mulaw_encode(samples)).decode("ascii"),
                },
                "streamSid": self._stream_sid,
            }
        )

    def closing(self) -> list[Message]:
        """A ``stop`` message: the caller hung up."""
        stop = {
            "event": "stop",
            "sequenceNumber": self._next_sequence(),
            "stop": {"accountSid": "AChecklr", "callSid": self._call_sid},
            "streamSid": self._stream_sid,
        }
        return [json.dumps(stop)]

    def read(self, message: Message) -> Inbound:
        """``media`` is audio, ``mark`` and ``clear`` as on Twilio; other events are ignored."""
        data = _json(message)
        match data.get("event"):
            case "media":
                payload = _field(data, "media", "payload")
                try:
                    encoded = base64.b64decode(payload, validate=True)
                except binascii.Error as exc:
                    raise AgentError(f"agent media payload is not base64: {exc}") from exc
                return AgentAudio(mulaw_decode(encoded), MULAW_RATE)
            case "mark":
                return AgentMark(_field(data, "mark", "name"))
            case "clear":
                return AgentClear()
        return None

    def mark_played(self, name: str) -> Message | None:
        """Echo the mark, as Twilio does once its audio has played or been cleared."""
        return json.dumps(
            {
                "event": "mark",
                "sequenceNumber": self._next_sequence(),
                "streamSid": self._stream_sid,
                "mark": {"name": name},
            }
        )

    def _next_sequence(self) -> str:
        self._sequence += 1
        return str(self._sequence)


def _field(data: dict[str, object], outer: str, inner: str) -> str:
    section = data.get(outer)
    value = section.get(inner) if isinstance(section, dict) else None
    if not isinstance(value, str):
        raise AgentError(f"agent {data.get('event')!r} message has no {outer}.{inner} text")
    return value


# --- pipecat ---------------------------------------------------------------------------------
# Pipecat's frames.proto, as far as a call needs it:
#   Frame { oneof frame { TextFrame text = 1; AudioRawFrame audio = 2; ...;
#                         InterruptionFrame interruption = 5; } }
#   AudioRawFrame { uint64 id = 1; string name = 2; bytes audio = 3; uint32 sample_rate = 4;
#                   uint32 num_channels = 5; optional uint64 pts = 6; }
# Two messages do not justify generated code and a protobuf runtime, so they are encoded here.

_FRAME_AUDIO: Final = 2
_FRAME_INTERRUPTION: Final = 5
_AUDIO_DATA: Final = 3
_AUDIO_RATE: Final = 4
_AUDIO_CHANNELS: Final = 5
_VARINT: Final = 0
_LENGTH_DELIMITED: Final = 2
_FIXED64: Final = 1
_FIXED32: Final = 5


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _key(field: int, wire_type: int) -> bytes:
    return _varint(field << 3 | wire_type)


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while offset < len(data):
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise AgentError("agent sent a truncated protobuf message")


def _fields(data: bytes) -> Iterator[tuple[int, int | bytes]]:
    """Top-level ``(field number, value)`` pairs; varints as ``int``, the rest as bytes."""
    offset = 0
    while offset < len(data):
        key, offset = _read_varint(data, offset)
        field, wire_type = key >> 3, key & 0x07
        if wire_type == _VARINT:
            value, offset = _read_varint(data, offset)
            yield field, value
            continue
        if wire_type == _LENGTH_DELIMITED:
            size, offset = _read_varint(data, offset)
        elif wire_type in (_FIXED64, _FIXED32):
            size = 8 if wire_type == _FIXED64 else 4
        else:
            raise AgentError(f"agent sent a protobuf field of unsupported wire type {wire_type}")
        if offset + size > len(data):
            raise AgentError("agent sent a truncated protobuf message")
        yield field, data[offset : offset + size]
        offset += size


class PipecatWire:
    """Pipecat's ``ProtobufFrameSerializer`` format."""

    def __init__(self, sample_rate: int) -> None:
        self._rate = sample_rate

    def opening(self) -> list[Message]:
        """Nothing: a Pipecat WebSocket pipeline starts on connect."""
        return []

    def audio(self, samples: Pcm16) -> Message:
        """An ``AudioRawFrame`` (Pipecat reads it as ``InputAudioRawFrame``)."""
        pcm = samples.astype("<i2").tobytes()
        frame = (
            _key(_AUDIO_DATA, _LENGTH_DELIMITED)
            + _varint(len(pcm))
            + pcm
            + _key(_AUDIO_RATE, _VARINT)
            + _varint(self._rate)
            + _key(_AUDIO_CHANNELS, _VARINT)
            + _varint(1)
        )
        return _key(_FRAME_AUDIO, _LENGTH_DELIMITED) + _varint(len(frame)) + frame

    def closing(self) -> list[Message]:
        """Nothing: closing the socket ends a Pipecat call."""
        return []

    def read(self, message: Message) -> Inbound:
        """Audio and interruption frames; text, transcription and app messages are ignored."""
        if isinstance(message, str):
            raise AgentError("agent sent text where Pipecat protobuf frames were expected")
        for field, value in _fields(message):
            if field == _FRAME_INTERRUPTION:
                return AgentClear()
            if field == _FRAME_AUDIO and isinstance(value, bytes):
                return _audio_frame(value)
        return None

    def mark_played(self, name: str) -> Message | None:
        """Pipecat's protocol has no marks."""
        return None


def _audio_frame(data: bytes) -> AgentAudio:
    audio, rate, channels = b"", 0, 1
    for field, value in _fields(data):
        if field == _AUDIO_DATA and isinstance(value, bytes):
            audio = value
        elif field == _AUDIO_RATE and isinstance(value, int):
            rate = value
        elif field == _AUDIO_CHANNELS and isinstance(value, int):
            channels = value
    if rate <= 0 or channels <= 0:
        raise AgentError(f"agent audio frame has sample rate {rate} and {channels} channel(s)")
    samples = _pcm(audio)
    if channels > 1:
        if len(samples) % channels:
            raise AgentError(f"agent audio frame does not hold whole {channels}-channel samples")
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
    return AgentAudio(samples, rate)
