"""Message formats, checked message by message and against Pipecat's own serializers."""

import base64
import json
import warnings

import numpy as np
import pytest

# Pipecat's audio helpers still import the stdlib ``audioop``, deprecated in Python 3.12.
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", "'audioop' is deprecated", DeprecationWarning)
    from pipecat.frames.frames import (
        InputAudioRawFrame,
        InterruptionFrame,
        OutputAudioRawFrame,
        TextFrame,
    )
    from pipecat.processors.frame_processor import FrameProcessorSetup
    from pipecat.serializers.protobuf import ProtobufFrameSerializer
    from pipecat.serializers.twilio import TwilioFrameSerializer

from hecklr_adapters import AgentError
from hecklr_adapters.voice.wire import (
    AgentAudio,
    AgentClear,
    AgentMark,
    PcmWire,
    PipecatWire,
    TwilioWire,
)
from hecklr_schemas import SessionContext, TrialId, new_id

CTX = SessionContext(trial_id=TrialId(new_id()), seed=1)
TONE = (3000 * np.sin(np.arange(320) / 5)).astype(np.int16)


def _audio(inbound: object) -> AgentAudio:
    assert isinstance(inbound, AgentAudio)
    return inbound


# --- pcm -------------------------------------------------------------------------------------


def test_pcm_wire_announces_the_format_and_carries_raw_samples() -> None:
    wire = PcmWire(CTX, "session-1", 24_000)
    [start] = wire.opening()
    assert json.loads(start) == {
        "type": "start",
        "session_id": "session-1",
        "trial_id": CTX.trial_id,
        "sample_rate": 24_000,
        "encoding": "pcm_s16le",
        "channels": 1,
    }
    message = wire.audio(TONE)
    assert message == TONE.astype("<i2").tobytes()
    heard = _audio(wire.read(message))
    assert np.array_equal(heard.samples, TONE)
    assert heard.sample_rate == 24_000
    assert json.loads(wire.closing()[0]) == {"type": "stop"}
    assert wire.mark_played("x") is None


def test_pcm_wire_reads_clear_and_ignores_other_control_messages() -> None:
    wire = PcmWire(CTX, "s", 16_000)
    assert isinstance(wire.read(json.dumps({"type": "clear"})), AgentClear)
    assert wire.read(json.dumps({"type": "transcript", "text": "hi"})) is None


@pytest.mark.parametrize(
    ("message", "error"),
    [(b"\x00", "odd"), ("{not json", "malformed JSON"), ("[1, 2]", "not an object")],
)
def test_pcm_wire_rejects_malformed_agent_messages(message: str | bytes, error: str) -> None:
    with pytest.raises(AgentError, match=error):
        PcmWire(CTX, "s", 16_000).read(message)


# --- twilio ----------------------------------------------------------------------------------


def test_twilio_wire_speaks_twilios_side_of_a_media_stream() -> None:
    wire = TwilioWire(CTX, "01J")
    connected, start = (json.loads(message) for message in wire.opening())
    assert connected == {"event": "connected", "protocol": "Call", "version": "1.0.0"}
    assert start["event"] == "start"
    assert start["streamSid"] == start["start"]["streamSid"] == "MZ01J"
    assert start["start"]["mediaFormat"] == {
        "encoding": "audio/x-mulaw",
        "sampleRate": 8000,
        "channels": 1,
    }
    assert start["start"]["customParameters"] == {"trial_id": CTX.trial_id}
    media = [json.loads(wire.audio(TONE[:160])) for _ in range(2)]
    assert [m["media"]["chunk"] for m in media] == ["1", "2"]
    assert [m["media"]["timestamp"] for m in media] == ["0", "20"]
    assert [m["sequenceNumber"] for m in media] == ["2", "3"]
    assert len(base64.b64decode(media[0]["media"]["payload"])) == 160
    [stop] = wire.closing()
    assert json.loads(stop)["event"] == "stop"
    mark = json.loads(wire.mark_played("reply-1") or "")
    assert mark["mark"] == {"name": "reply-1"}
    assert mark["streamSid"] == "MZ01J"


def test_twilio_wire_reads_media_mark_and_clear() -> None:
    wire = TwilioWire(CTX, "s")
    payload = base64.b64encode(bytes([0xFF, 0x7F, 0x00])).decode()
    heard = _audio(wire.read(json.dumps({"event": "media", "media": {"payload": payload}})))
    assert heard.sample_rate == 8000
    assert heard.samples.tolist()[:2] == [0, 0]  # mu-law 0xFF and 0x7F are silence
    mark = wire.read(json.dumps({"event": "mark", "mark": {"name": "m1"}}))
    assert mark == AgentMark("m1")
    assert isinstance(wire.read(json.dumps({"event": "clear"})), AgentClear)
    assert wire.read(json.dumps({"event": "dtmf"})) is None


@pytest.mark.parametrize(
    ("message", "error"),
    [
        (b"\x00\x01", "binary"),
        (json.dumps({"event": "media", "media": {"payload": "%%%"}}), "base64"),
        (json.dumps({"event": "media"}), "media.payload"),
        (json.dumps({"event": "mark", "mark": {}}), "mark.name"),
    ],
)
def test_twilio_wire_rejects_malformed_agent_messages(message: str | bytes, error: str) -> None:
    with pytest.raises(AgentError, match=error):
        TwilioWire(CTX, "s").read(message)


@pytest.mark.anyio
async def test_twilio_wire_interoperates_with_pipecats_twilio_serializer() -> None:
    serializer = TwilioFrameSerializer(
        "MZ01J", params=TwilioFrameSerializer.InputParams(auto_hang_up=False)
    )
    await serializer.setup(_setup(16_000))
    wire = TwilioWire(CTX, "01J")
    tone = (3000 * np.sin(np.arange(3200) / 7)).astype(np.int16)
    frame = await serializer.deserialize(wire.audio(tone[::2]))
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.sample_rate == 16_000
    out = await serializer.serialize(OutputAudioRawFrame(tone.tobytes(), 16_000, 1))
    assert isinstance(out, str)
    heard = _audio(wire.read(out))
    assert heard.sample_rate == 8000
    assert 0 < len(heard.samples) <= 1600  # Pipecat's soxr resampler holds some back
    clear = await serializer.serialize(InterruptionFrame())
    assert isinstance(clear, str)
    assert isinstance(wire.read(clear), AgentClear)


# --- pipecat ---------------------------------------------------------------------------------


def _setup(sample_rate: int) -> FrameProcessorSetup:
    """What a pipeline hands its serializer at start-up (only the rates matter here)."""

    class Setup:
        audio_in_sample_rate = sample_rate
        audio_out_sample_rate = sample_rate

    return Setup()  # type: ignore[return-value]  # duck-typed stand-in for the pipeline setup


@pytest.mark.anyio
async def test_pipecat_wire_round_trips_through_pipecats_protobuf_serializer() -> None:
    serializer = ProtobufFrameSerializer()
    wire = PipecatWire(16_000)
    frame = await serializer.deserialize(wire.audio(TONE))
    assert isinstance(frame, InputAudioRawFrame)
    assert frame.sample_rate == 16_000
    assert frame.num_channels == 1
    assert frame.audio == TONE.tobytes()
    data = await serializer.serialize(OutputAudioRawFrame(TONE.tobytes(), 24_000, 1))
    assert isinstance(data, bytes)
    heard = _audio(wire.read(data))
    assert np.array_equal(heard.samples, TONE)
    assert heard.sample_rate == 24_000
    interruption = await serializer.serialize(InterruptionFrame())
    assert isinstance(interruption, bytes)
    assert isinstance(wire.read(interruption), AgentClear)
    text = await serializer.serialize(TextFrame("hello"))
    assert isinstance(text, bytes)
    assert wire.read(text) is None
    assert wire.opening() == wire.closing() == []
    assert wire.mark_played("x") is None


@pytest.mark.anyio
async def test_pipecat_wire_mixes_stereo_down_to_mono() -> None:
    stereo = np.stack([TONE, np.zeros_like(TONE)], axis=1).reshape(-1)
    data = await ProtobufFrameSerializer().serialize(
        OutputAudioRawFrame(stereo.astype("<i2").tobytes(), 16_000, 2)
    )
    assert isinstance(data, bytes)
    heard = _audio(PipecatWire(16_000).read(data))
    assert np.array_equal(heard.samples, (TONE / 2).astype(np.int16))


@pytest.mark.parametrize(
    ("message", "error"),
    [
        ("text", "text"),
        (b"\x12\x05\x1a\x02\x00", "truncated"),
        (b"\x12", "truncated"),
        (b"\x13", "wire type"),
        (b"\x12\x02\x20\x00", "sample rate 0"),
    ],
)
def test_pipecat_wire_rejects_malformed_frames(message: str | bytes, error: str) -> None:
    with pytest.raises(AgentError, match=error):
        PipecatWire(16_000).read(message)


def test_pipecat_wire_skips_fixed_width_fields() -> None:
    # Frame { audio { id(fixed64-encoded as unknown) ...} } with an unknown fixed32 field first.
    audio = b"\x1a\x02\x01\x00\x20\x80\x7d\x28\x01"
    message = b"\x35\x00\x00\x00\x00" + b"\x12" + bytes([len(audio)]) + audio
    heard = _audio(PipecatWire(16_000).read(message))
    assert heard.samples.tolist() == [1]
    assert heard.sample_rate == 16_000


def test_pipecat_wire_rejects_partial_multichannel_samples() -> None:
    audio = b"\x1a\x02\x01\x00\x20\x80\x7d\x28\x02"
    with pytest.raises(AgentError, match="2-channel"):
        PipecatWire(16_000).read(b"\x12" + bytes([len(audio)]) + audio)
