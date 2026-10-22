"""AssemblyAI ASR: Universal-Streaming over the v3 WebSocket, or the pre-recorded REST API.

The engine hands its ASR one whole agent turn once the VAD closes it. ``assemblyai/<model>``
streams each turn through its own session: send the PCM, ``Terminate``, and read every ``Turn``
until ``Termination``. Only ``Terminate`` flushes the transcript of audio sent faster than real
time (``ForceEndpoint`` is handled ahead of queued audio), so a session cannot be reused for the
next turn without risking a cut transcript. ``assemblyai-batch/<model>`` uploads the turn and
polls the transcript instead; it has no per-minute session limit.

Prices published in October 2026, per hour: streaming bills session duration, USD 0.15 for
``universal-streaming-*`` and 0.45 for ``universal-3-*-pro``; pre-recorded bills audio, USD 0.15
for ``universal-2`` and 0.21 for ``universal-3-5-pro``. Free accounts open 5 streaming sessions
per minute, which a single call exceeds; retrying a refused session did not get through within a
turn's deadline (refused attempts appear to count too), so the refusal is reported at once with
the batch provider as the way out.
"""

import contextlib
import io
import json
import wave
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlencode

import anyio
import httpx2
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidStatus, WebSocketException

from hecklr_voice.asr import Transcript
from hecklr_voice.audio import SAMPLE_RATE, Pcm16
from hecklr_voice.errors import VoiceConfigError, VoiceProviderError

KEY_ENV: Final = "ASSEMBLYAI_API_KEY"
ENDPOINT: Final = "wss://streaming.assemblyai.com/v3/ws"
API_BASE: Final = "https://api.assemblyai.com"
DEFAULT_MODEL: Final = "universal-streaming-english"
DEFAULT_BATCH_MODEL: Final = "universal-2"
CHUNK_SAMPLES: Final = SAMPLE_RATE // 10
"""100 ms per binary frame; AssemblyAI accepts 50 to 1000 ms."""
MIN_CHUNK_SAMPLES: Final = SAMPLE_RATE // 20
"""A shorter last frame is padded with silence up to the 50 ms minimum."""
RATE_LIMITED: Final = "Too many concurrent sessions"
"""AssemblyAI's error text when an account opens sessions faster than its per-minute limit."""
POLL_INTERVAL_S: Final = 0.25
_QUEUE_FRAMES: Final = 4_096
# Audio is sent before any reply is read. With websockets' default 16-frame queue, partial
# transcripts arriving meanwhile would pause the socket; a turn produces far fewer than this.


def _key(environ: Mapping[str, str], provider: str) -> str:
    key = environ.get(KEY_ENV)
    if not key:
        raise VoiceConfigError(f"the {provider} ASR provider needs {KEY_ENV} to be set")
    return key


# --- streaming --------------------------------------------------------------------------------


@dataclass(slots=True)
class _Turn:
    final: bool
    text: str
    confidences: list[float]


@dataclass(slots=True)
class _Turns:
    """The latest message of every ``turn_order``, keeping what was already heard.

    A partial never replaces a final, and an empty transcript never replaces words: Universal
    Streaming may close an unfinished turn with an empty final, and with ``format_turns`` the
    formatted final follows the unformatted one.
    """

    by_order: dict[int, _Turn] = field(default_factory=dict)

    def add(self, event: Mapping[str, Any]) -> None:
        order = int(event["turn_order"])
        text = str(event.get("transcript", "")).strip()
        final = bool(event.get("end_of_turn"))
        current = self.by_order.get(order)
        if current is not None and ((current.final and not final) or (current.text and not text)):
            return
        words = event.get("words") or []
        confidences = [float(word["confidence"]) for word in words if "confidence" in word]
        self.by_order[order] = _Turn(final, text, confidences)

    def transcript(self) -> Transcript:
        turns = [self.by_order[order] for order in sorted(self.by_order)]
        text = " ".join(turn.text for turn in turns if turn.text)
        if not text:
            return Transcript("", 0.0)
        confidences = [value for turn in turns for value in turn.confidences]
        return Transcript(text, sum(confidences) / len(confidences) if confidences else None)


class AssemblyAIASR:
    """AssemblyAI streaming speech-to-text; ``model`` is the ``speech_model`` connection value.

    ``universal-streaming-english`` and ``universal-streaming-multilingual`` are asked for
    formatted turns (punctuation, casing); the ``universal-3-*-pro`` models always format.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        *,
        url: str = ENDPOINT,
        timeout_s: float = 30.0,
    ) -> None:
        self._key = api_key
        self._model = model
        self._url = url
        self._timeout_s = timeout_s

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str], model: str = DEFAULT_MODEL, *, url: str = ENDPOINT
    ) -> "AssemblyAIASR":
        """Build from ``ASSEMBLYAI_API_KEY``; refuse to start without it."""
        return cls(_key(environ, "assemblyai"), model, url=url)

    @property
    def name(self) -> str:
        """``assemblyai/<speech model>``."""
        return f"assemblyai/{self._model}"

    def session_url(self) -> str:
        """The WebSocket URL with this provider's connection parameters."""
        params = {"sample_rate": SAMPLE_RATE, "encoding": "pcm_s16le", "speech_model": self._model}
        if self._model.startswith("universal-streaming"):
            params["format_turns"] = "true"
        return f"{self._url}?{urlencode(params)}"

    async def transcribe(self, pcm: Pcm16, *, language: str) -> Transcript:
        """Stream ``pcm`` through one session and join its turns.

        ``language`` is not sent: the model fixes the language (``-english``) or detects it
        (``-multilingual`` and the pro models).

        Raises:
            VoiceProviderError: refused connection (including the per-minute session limit),
                server error, early close, or no transcript before the deadline.
        """
        if not len(pcm):
            return Transcript("", 0.0)
        # The server reads audio at about 1.25x real time, so allow the audio's own length too.
        deadline_s = self._timeout_s + len(pcm) / SAMPLE_RATE
        try:
            with anyio.fail_after(deadline_s):
                return await self._session(pcm)
        except TimeoutError as exc:
            raise VoiceProviderError(
                f"assemblyai gave no transcript within {deadline_s:.0f} s"
            ) from exc

    async def _session(self, pcm: Pcm16) -> Transcript:
        try:
            async with connect(
                self.session_url(),
                additional_headers={"Authorization": self._key},
                max_queue=_QUEUE_FRAMES,
                open_timeout=self._timeout_s,
            ) as session:
                # A server that closes mid-send says why in an Error message first: read it.
                with contextlib.suppress(ConnectionClosed):
                    await _send_turn(session, pcm)
                return await _read_until_termination(session)
        except InvalidStatus as exc:
            status = exc.response.status_code
            raise VoiceProviderError(f"assemblyai refused the session: HTTP {status}") from exc
        except ConnectionClosed as exc:
            code, reason = (exc.rcvd.code, exc.rcvd.reason) if exc.rcvd else (None, "")
            raise VoiceProviderError(f"assemblyai closed the session: {code} {reason}") from exc
        except (OSError, WebSocketException) as exc:
            raise VoiceProviderError(f"assemblyai connection failed: {exc}") from exc


async def _send_turn(session: ClientConnection, pcm: Pcm16) -> None:
    for start in range(0, len(pcm), CHUNK_SAMPLES):
        chunk = pcm[start : start + CHUNK_SAMPLES].astype("<i2").tobytes()
        missing = MIN_CHUNK_SAMPLES * 2 - len(chunk)
        await session.send(chunk + bytes(max(0, missing)))
    await session.send(json.dumps({"type": "Terminate"}))


async def _read_until_termination(session: ClientConnection) -> Transcript:
    turns = _Turns()
    async for message in session:
        try:
            event = json.loads(message)
            kind = event.get("type")
            if kind == "Turn":
                turns.add(event)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise VoiceProviderError(f"assemblyai sent an unexpected message: {exc}") from exc
        if kind == "Error":
            code, error = event.get("error_code"), str(event.get("error"))
            hint = ""
            if RATE_LIMITED.casefold() in error.casefold():
                hint = (
                    " (the account's new streaming sessions per minute are used up, 5 on a free"
                    " account; each agent turn opens one: use assemblyai-batch/universal-2)"
                )
            raise VoiceProviderError(f"assemblyai error {code}: {error}{hint}")
        if kind == "Termination":
            return turns.transcript()
    raise VoiceProviderError("assemblyai closed the session before Termination")


# --- pre-recorded -----------------------------------------------------------------------------


def wav_bytes(pcm: Pcm16) -> bytes:
    """A 16 kHz mono 16-bit WAV file of ``pcm``: the upload endpoint needs a container."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm.astype("<i2").tobytes())
    return buffer.getvalue()


class AssemblyAIBatchASR:
    """AssemblyAI pre-recorded speech-to-text: upload the turn, submit it, poll the transcript."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_BATCH_MODEL,
        *,
        base_url: str = API_BASE,
        timeout_s: float = 30.0,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._model = model
        self._base = base_url
        self._timeout_s = timeout_s
        self._transport = transport

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str],
        model: str = DEFAULT_BATCH_MODEL,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> "AssemblyAIBatchASR":
        """Build from ``ASSEMBLYAI_API_KEY``; refuse to start without it."""
        return cls(_key(environ, "assemblyai-batch"), model, transport=transport)

    @property
    def name(self) -> str:
        """``assemblyai-batch/<speech model>``."""
        return f"assemblyai-batch/{self._model}"

    async def transcribe(self, pcm: Pcm16, *, language: str) -> Transcript:
        """Transcribe one turn; ``language``'s primary subtag becomes ``language_code``.

        Raises:
            VoiceProviderError: a failed request, a failed job, or no result before the deadline.
        """
        if not len(pcm):
            return Transcript("", 0.0)
        deadline_s = self._timeout_s + len(pcm) / SAMPLE_RATE
        try:
            with anyio.fail_after(deadline_s):
                async with httpx2.AsyncClient(
                    base_url=self._base,
                    headers={"Authorization": self._key},
                    transport=self._transport,
                    timeout=self._timeout_s,
                ) as client:
                    return await self._job(client, pcm, language)
        except TimeoutError as exc:
            raise VoiceProviderError(
                f"assemblyai-batch gave no transcript within {deadline_s:.0f} s"
            ) from exc
        except httpx2.HTTPError as exc:
            raise VoiceProviderError(f"assemblyai-batch request failed: {exc}") from exc
        except (KeyError, TypeError, ValueError) as exc:
            raise VoiceProviderError(
                f"assemblyai-batch returned an unexpected body: {exc}"
            ) from exc

    async def _job(self, client: httpx2.AsyncClient, pcm: Pcm16, language: str) -> Transcript:
        upload = _json(await client.post("/v2/upload", content=wav_bytes(pcm)))
        submitted = _json(
            await client.post(
                "/v2/transcript",
                json={
                    "audio_url": upload["upload_url"],
                    "speech_models": [self._model],
                    "language_code": language.split("-")[0].lower(),
                },
            )
        )
        while True:
            job = _json(await client.get(f"/v2/transcript/{submitted['id']}"))
            if job["status"] == "completed":
                text = str(job.get("text") or "").strip()
                if not text:
                    return Transcript("", 0.0)
                confidence = job.get("confidence")
                return Transcript(text, None if confidence is None else float(confidence))
            if job["status"] == "error":
                raise VoiceProviderError(f"assemblyai-batch job failed: {job.get('error')}")
            await anyio.sleep(POLL_INTERVAL_S)


def _json(response: httpx2.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise VoiceProviderError(
            f"assemblyai-batch returned HTTP {response.status_code}: {response.text[:200]}"
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise VoiceProviderError(f"assemblyai-batch returned a non-JSON body: {exc}") from exc
    if not isinstance(body, dict):
        raise VoiceProviderError("assemblyai-batch returned an unexpected body")
    return body
