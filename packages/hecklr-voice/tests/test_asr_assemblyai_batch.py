"""AssemblyAI pre-recorded ASR (``assemblyai-batch``) against a mocked REST API."""

import io
import json
import wave
from collections.abc import Callable

import httpx2
import numpy as np
import pytest

from hecklr_voice.asr_assemblyai import AssemblyAIBatchASR
from hecklr_voice.audio import SAMPLE_RATE
from hecklr_voice.errors import VoiceConfigError, VoiceProviderError
from hecklr_voice.providers import make_asr

pytestmark = pytest.mark.anyio

KEY = "aai-secret-key"
Handler = Callable[[httpx2.Request], httpx2.Response]


def _api(statuses: list[dict[str, object]], seen: list[httpx2.Request]) -> Handler:
    """Upload, submit, then one poll response per entry of ``statuses`` (the last repeats)."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        match request.method, request.url.path:
            case "POST", "/v2/upload":
                return httpx2.Response(200, json={"upload_url": "https://cdn.test/u1"})
            case "POST", "/v2/transcript":
                return httpx2.Response(200, json={"id": "t1", "status": "queued"})
            case "GET", "/v2/transcript/t1":
                return httpx2.Response(
                    200, json=statuses.pop(0) if len(statuses) > 1 else statuses[0]
                )
        return httpx2.Response(404)

    return handle


def _asr(handler: Handler, *, timeout_s: float = 30.0) -> AssemblyAIBatchASR:
    return AssemblyAIBatchASR(KEY, transport=httpx2.MockTransport(handler), timeout_s=timeout_s)


def _speech(seconds: float) -> np.ndarray:
    return (np.arange(int(seconds * SAMPLE_RATE)) % 500).astype(np.int16)


async def test_a_turn_is_uploaded_as_wav_submitted_and_polled() -> None:
    seen: list[httpx2.Request] = []
    done = {"status": "completed", "text": " Booked for Thursday. ", "confidence": 0.93}
    asr = _asr(_api([{"status": "processing"}, done], seen))
    pcm = _speech(0.5)

    heard = await asr.transcribe(pcm, language="en-GB")

    assert (heard.text, heard.confidence) == ("Booked for Thursday.", 0.93)
    upload, submit, *polls = seen
    assert all(request.headers["Authorization"] == KEY for request in seen)
    with wave.open(io.BytesIO(upload.content)) as wav:
        assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (SAMPLE_RATE, 1, 2)
        assert np.array_equal(np.frombuffer(wav.readframes(len(pcm)), dtype="<i2"), pcm)
    assert json.loads(submit.content) == {
        "audio_url": "https://cdn.test/u1",
        "speech_models": ["universal-2"],
        "language_code": "en",
    }
    assert len(polls) == 2


async def test_nothing_heard_is_empty_text_with_zero_confidence() -> None:
    asr = _asr(_api([{"status": "completed", "text": None, "confidence": None}], []))
    heard = await asr.transcribe(_speech(0.2), language="en-US")
    assert (heard.text, heard.confidence) == ("", 0.0)


async def test_unknown_confidence_stays_unknown() -> None:
    asr = _asr(_api([{"status": "completed", "text": "Hello."}], []))
    heard = await asr.transcribe(_speech(0.2), language="en-US")
    assert (heard.text, heard.confidence) == ("Hello.", None)


async def test_empty_audio_is_silence_without_a_request() -> None:
    seen: list[httpx2.Request] = []
    heard = await _asr(_api([], seen)).transcribe(np.zeros(0, np.int16), language="en-US")
    assert (heard.text, heard.confidence, seen) == ("", 0.0, [])


def _status(code: int, body: str) -> Handler:
    return lambda request: httpx2.Response(code, text=body)


def _failing(request: httpx2.Request) -> httpx2.Response:
    raise httpx2.ConnectError("unreachable", request=request)


@pytest.mark.parametrize(
    ("handler", "message"),
    [
        (_api([{"status": "error", "error": "Transcoding failed"}], []), "job failed: Transc"),
        (_status(401, "Invalid API key"), "HTTP 401: Invalid API key"),
        (_status(200, "<html>"), "non-JSON body"),
        (_status(200, "[]"), "unexpected body"),
        (lambda request: httpx2.Response(200, json={"no": "url"}), "unexpected body"),
        (_failing, "request failed"),
    ],
)
async def test_failures_are_provider_errors_without_the_key(handler: Handler, message: str) -> None:
    with pytest.raises(VoiceProviderError, match=message) as caught:
        await _asr(handler).transcribe(_speech(0.2), language="en-US")
    assert KEY not in str(caught.value)


async def test_a_job_that_never_completes_times_out() -> None:
    asr = _asr(_api([{"status": "processing"}], []), timeout_s=0.3)
    with pytest.raises(VoiceProviderError, match="no transcript within"):
        await asr.transcribe(_speech(0.1), language="en-US")


def test_make_asr_builds_the_batch_provider_from_the_environment() -> None:
    asr = make_asr("assemblyai-batch/universal-3-5-pro", environ={"ASSEMBLYAI_API_KEY": KEY})
    assert isinstance(asr, AssemblyAIBatchASR)
    assert asr.name == "assemblyai-batch/universal-3-5-pro"
    with pytest.raises(VoiceConfigError, match="ASSEMBLYAI_API_KEY"):
        make_asr("assemblyai-batch/universal-2", environ={})
