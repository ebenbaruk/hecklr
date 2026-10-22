"""AssemblyAI streaming ASR against a local server that speaks the v3 message sequence."""

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import numpy as np
import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from hecklr_voice.asr import ASR
from hecklr_voice.asr_assemblyai import (
    CHUNK_SAMPLES,
    MIN_CHUNK_SAMPLES,
    RATE_LIMITED,
    AssemblyAIASR,
)
from hecklr_voice.audio import SAMPLE_RATE
from hecklr_voice.errors import VoiceConfigError, VoiceProviderError
from hecklr_voice.providers import make_asr

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.enable_socket,
    pytest.mark.allow_hosts(["127.0.0.1"]),
]

KEY = "aai-secret-key"


def _turn(order: int, text: str, *, final: bool, formatted: bool = False, **extra: Any) -> str:
    event = {
        "type": "Turn",
        "turn_order": order,
        "end_of_turn": final,
        "turn_is_formatted": formatted,
        "transcript": text,
        "words": [{"text": w, "confidence": 0.9} for w in text.split()],
    }
    return json.dumps(event | extra)


@dataclass
class Session:
    """What the mock server saw of one session."""

    path: str = ""
    headers: Headers = field(default_factory=Headers)
    frames: list[bytes] = field(default_factory=list)
    controls: list[str] = field(default_factory=list)


Script = Callable[[ServerConnection, Session], Awaitable[None]]


async def _receive_until_terminate(ws: ServerConnection, session: Session) -> None:
    async for message in ws:
        if isinstance(message, bytes):
            session.frames.append(message)
            continue
        session.controls.append(json.loads(message)["type"])
        if session.controls[-1] == "Terminate":
            return


@asynccontextmanager
async def mock_server(
    script: Script,
    process_request: Callable[[ServerConnection, Request], Response | None] | None = None,
) -> AsyncIterator[tuple[str, list[Session]]]:
    sessions: list[Session] = []

    async def handler(ws: ServerConnection) -> None:
        assert ws.request is not None
        session = Session(ws.request.path, ws.request.headers)
        sessions.append(session)
        await script(ws, session)

    async with serve(handler, "127.0.0.1", 0, process_request=process_request) as server:
        port = server.sockets[0].getsockname()[1]
        yield f"ws://127.0.0.1:{port}/v3/ws", sessions


def _speech(seconds: float) -> np.ndarray:
    return np.full(int(seconds * SAMPLE_RATE), 1_000, dtype=np.int16)


async def _two_turns(ws: ServerConnection, session: Session) -> None:
    await ws.send(json.dumps({"type": "Begin", "id": "s1", "expires_at": 0}))
    await ws.send(_turn(0, "your visit is", final=False))
    await ws.send(_turn(0, "your visit is booked", final=True))
    await ws.send(_turn(0, "Your visit is booked.", final=True, formatted=True))
    await _receive_until_terminate(ws, session)
    await ws.send(_turn(1, "for thursday", final=False))
    await ws.send(_turn(1, "For Thursday.", final=True, formatted=True))
    # Universal Streaming may close an unfinished turn with an empty final.
    await ws.send(_turn(1, "", final=True))
    await ws.send(json.dumps({"type": "Termination", "audio_duration_seconds": 1}))


async def test_transcribe_joins_formatted_finals_in_turn_order() -> None:
    async with mock_server(_two_turns) as (url, sessions):
        asr = AssemblyAIASR(KEY, url=url)
        heard = await asr.transcribe(_speech(1.0), language="en-US")

    assert heard.text == "Your visit is booked. For Thursday."
    assert heard.confidence == pytest.approx(0.9)
    (session,) = sessions
    assert session.headers["Authorization"] == KEY
    assert session.controls == ["Terminate"]


async def test_session_parameters_match_the_harness_audio_format() -> None:
    async with mock_server(_two_turns) as (url, sessions):
        await AssemblyAIASR(KEY, url=url).transcribe(_speech(0.2), language="en-US")
        await AssemblyAIASR(KEY, "universal-3-6-pro", url=url).transcribe(
            _speech(0.2), language="en-US"
        )

    streaming, pro = (parse_qs(urlsplit(s.path).query) for s in sessions)
    assert streaming == {
        "sample_rate": ["16000"],
        "encoding": ["pcm_s16le"],
        "speech_model": ["universal-streaming-english"],
        "format_turns": ["true"],
    }
    # The pro models always format and take no format_turns.
    assert "format_turns" not in pro
    assert pro["speech_model"] == ["universal-3-6-pro"]


async def test_audio_goes_in_frames_of_50_to_1000_ms_with_every_sample() -> None:
    pcm = (np.arange(int(1.03 * SAMPLE_RATE)) % 2_000).astype(np.int16)
    async with mock_server(_two_turns) as (url, sessions):
        await AssemblyAIASR(KEY, url=url).transcribe(pcm, language="en-US")

    frames = sessions[0].frames
    sizes = [len(frame) // 2 for frame in frames]
    assert all(MIN_CHUNK_SAMPLES <= size <= CHUNK_SAMPLES for size in sizes)
    sent = np.frombuffer(b"".join(frames), dtype="<i2")
    # The 30 ms tail is padded with silence up to the 50 ms minimum.
    assert np.array_equal(sent[: len(pcm)], pcm)
    assert not sent[len(pcm) :].any()
    assert len(sent) == 10 * CHUNK_SAMPLES + MIN_CHUNK_SAMPLES


async def test_empty_audio_is_silence_without_a_session() -> None:
    asr = AssemblyAIASR(KEY, url="ws://127.0.0.1:9/never")
    heard = await asr.transcribe(np.zeros(0, dtype=np.int16), language="en-US")
    assert (heard.text, heard.confidence) == ("", 0.0)


async def test_nothing_heard_is_empty_text_with_zero_confidence() -> None:
    async def silent(ws: ServerConnection, session: Session) -> None:
        await _receive_until_terminate(ws, session)
        await ws.send(_turn(0, "", final=True))
        await ws.send(json.dumps({"type": "Termination"}))

    async with mock_server(silent) as (url, _):
        heard = await AssemblyAIASR(KEY, url=url).transcribe(_speech(0.5), language="en-US")
    assert (heard.text, heard.confidence) == ("", 0.0)


async def test_a_turn_without_word_confidences_has_unknown_confidence() -> None:
    async def wordless(ws: ServerConnection, session: Session) -> None:
        await _receive_until_terminate(ws, session)
        await ws.send(_turn(0, "Hello.", final=True, formatted=True, words=[]))
        await ws.send(json.dumps({"type": "Termination"}))

    async with mock_server(wordless) as (url, _):
        heard = await AssemblyAIASR(KEY, url=url).transcribe(_speech(0.5), language="en-US")
    assert (heard.text, heard.confidence) == ("Hello.", None)


async def test_a_partial_never_replaces_a_final_of_the_same_turn() -> None:
    async def late_partial(ws: ServerConnection, session: Session) -> None:
        await _receive_until_terminate(ws, session)
        await ws.send(_turn(0, "Booked.", final=True, formatted=True))
        await ws.send(_turn(0, "boo", final=False))
        await ws.send(_turn(1, "only partial", final=False))
        await ws.send(json.dumps({"type": "Termination"}))

    async with mock_server(late_partial) as (url, _):
        heard = await AssemblyAIASR(KEY, url=url).transcribe(_speech(0.5), language="en-US")
    # A turn that never got a final still counts: its words were spoken.
    assert heard.text == "Booked. only partial"


async def _error(ws: ServerConnection, session: Session) -> None:
    await _receive_until_terminate(ws, session)
    await ws.send(json.dumps({"type": "Error", "error_code": 3005, "error": "Session Cancelled"}))
    await ws.close(3005, "Session Cancelled")


async def _closes_with_policy_violation(ws: ServerConnection, session: Session) -> None:
    await ws.close(1008, "Unauthorized Connection: insufficient balance")


async def _closes_before_termination(ws: ServerConnection, session: Session) -> None:
    await _receive_until_terminate(ws, session)
    await ws.send(_turn(0, "Hello.", final=True, formatted=True))


async def _sends_garbage(ws: ServerConnection, session: Session) -> None:
    await _receive_until_terminate(ws, session)
    await ws.send("not json")


@pytest.mark.parametrize(
    ("script", "message"),
    [
        (_error, "assemblyai error 3005: Session Cancelled"),
        (_closes_with_policy_violation, "1008 Unauthorized Connection: insufficient balance"),
        (_closes_before_termination, "closed the session before Termination"),
        (_sends_garbage, "unexpected message"),
    ],
)
async def test_server_failures_are_provider_errors_without_the_key(
    script: Script, message: str
) -> None:
    async with mock_server(script) as (url, _):
        with pytest.raises(VoiceProviderError, match=message) as caught:
            await AssemblyAIASR(KEY, url=url).transcribe(_speech(0.3), language="en-US")
    assert KEY not in str(caught.value)


async def _refuse_at_once(ws: ServerConnection, session: Session) -> None:
    error = "Unauthorized Connection: insufficient balance"
    await ws.send(json.dumps({"type": "Error", "error_code": 1008, "error": error}))
    await ws.close(1008, "See Error message for details")


async def test_the_error_is_reported_when_the_server_closes_while_audio_is_sent() -> None:
    async with mock_server(_refuse_at_once) as (url, _):
        with pytest.raises(VoiceProviderError, match="1008: Unauthorized Connection: insufficient"):
            await AssemblyAIASR(KEY, url=url).transcribe(_speech(10.0), language="en-US")


async def _rate_limited(ws: ServerConnection, session: Session) -> None:
    await ws.send(json.dumps({"type": "Error", "error_code": 1008, "error": RATE_LIMITED}))
    await ws.close(1008, "See Error message for details")


async def test_the_session_limit_is_reported_with_the_batch_provider_as_the_way_out() -> None:
    async with mock_server(_rate_limited) as (url, sessions):
        with pytest.raises(VoiceProviderError, match="use assemblyai-batch/universal-2"):
            await AssemblyAIASR(KEY, url=url).transcribe(_speech(0.3), language="en-US")
    assert len(sessions) == 1


async def test_a_refused_handshake_reports_the_http_status() -> None:
    def refuse(ws: ServerConnection, request: Request) -> Response:
        return ws.respond(401, "Unauthorized")

    async with mock_server(_two_turns, process_request=refuse) as (url, _):
        with pytest.raises(VoiceProviderError, match="HTTP 401"):
            await AssemblyAIASR(KEY, url=url).transcribe(_speech(0.3), language="en-US")


async def test_a_session_that_never_terminates_times_out() -> None:
    async def stalls(ws: ServerConnection, session: Session) -> None:
        await _receive_until_terminate(ws, session)
        await ws.wait_closed()

    async with mock_server(stalls) as (url, _):
        asr = AssemblyAIASR(KEY, url=url, timeout_s=0.2)
        with pytest.raises(VoiceProviderError, match="no transcript within"):
            await asr.transcribe(_speech(0.1), language="en-US")


async def test_an_unreachable_endpoint_is_a_provider_error() -> None:
    async with mock_server(_two_turns) as (url, _):
        closed_url = url
    with pytest.raises(VoiceProviderError, match="connection failed"):
        await AssemblyAIASR(KEY, url=closed_url).transcribe(_speech(0.1), language="en-US")


def test_make_asr_builds_assemblyai_from_the_environment() -> None:
    asr = make_asr("assemblyai/universal-streaming-english", environ={"ASSEMBLYAI_API_KEY": KEY})
    assert isinstance(asr, AssemblyAIASR)
    assert isinstance(asr, ASR)
    assert asr.name == "assemblyai/universal-streaming-english"
    with pytest.raises(VoiceConfigError, match="ASSEMBLYAI_API_KEY") as caught:
        make_asr("assemblyai/universal-streaming-english", environ={})
    assert KEY not in str(caught.value)
