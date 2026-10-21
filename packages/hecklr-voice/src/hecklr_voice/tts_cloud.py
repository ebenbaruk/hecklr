"""Cloud TTS over plain HTTP: Cartesia, ElevenLabs and OpenAI, each asked for raw 16-bit PCM.

They are real clients, but only usable when the provider's API key is in the environment; unit
tests drive them through a mocked transport. Each provider supports a different speaking-rate
range and none of them a pitch shift, so out-of-range settings are refused rather than clamped.
"""

from collections.abc import Mapping
from typing import ClassVar, Final, Self

import httpx2

from hecklr_voice.audio import Audio, bytes_to_pcm16, from_pcm16, resample
from hecklr_voice.errors import VoiceConfigError, VoiceProviderError
from hecklr_voice.pool import run_dsp
from hecklr_voice.tts import SpeechRequest

_TIMEOUT_S: Final = 30.0


def decode_pcm16(content: bytes, sample_rate: int) -> Audio:
    """Pool job: little-endian 16-bit PCM at ``sample_rate`` to float audio at 16 kHz."""
    return resample(from_pcm16(bytes_to_pcm16(content)), sample_rate, 16_000)


class _HttpTTS:
    """Shared plumbing: key lookup, settings checks, one POST per utterance, PCM decoding."""

    provider: ClassVar[str]
    key_env: ClassVar[str]
    rate_range: ClassVar[tuple[float, float]]
    sample_rate: ClassVar[int]

    def __init__(
        self, api_key: str, *, model: str, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        self._key = api_key
        self._model = model
        self._transport = transport

    @classmethod
    def from_environ(
        cls, environ: Mapping[str, str], *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> Self:
        """Build from the provider's API key variable; refuse to start without it."""
        key = environ.get(cls.key_env)
        if not key:
            raise VoiceConfigError(f"TTS provider {cls.provider!r} needs {cls.key_env} to be set")
        return cls(key, model=cls.default_model(), transport=transport)

    @classmethod
    def default_model(cls) -> str:
        """The model used when none is configured."""
        raise NotImplementedError

    @property
    def name(self) -> str:
        """``provider/model``."""
        return f"{self.provider}/{self._model}"

    async def synthesize(self, request: SpeechRequest) -> Audio:
        """POST the text, then decode the PCM reply in a worker process."""
        self._check(request)
        url, headers, params, body = self._request(request)
        try:
            async with httpx2.AsyncClient(
                transport=self._transport, timeout=httpx2.Timeout(_TIMEOUT_S)
            ) as client:
                response = await client.post(url, headers=headers, params=params, json=body)
        except httpx2.HTTPError as exc:
            raise VoiceProviderError(f"{self.provider} TTS request failed: {exc}") from exc
        if response.status_code != 200:
            raise VoiceProviderError(
                f"{self.provider} TTS returned HTTP {response.status_code}: {response.text[:200]}"
            )
        return await run_dsp(decode_pcm16, response.content, self.sample_rate)

    def _check(self, request: SpeechRequest) -> None:
        low, high = self.rate_range
        if not low <= request.rate <= high:
            raise VoiceConfigError(
                f"{self.provider} supports rate {low}-{high}, persona asks for {request.rate}"
            )
        if request.pitch_semitones != 0:
            raise VoiceConfigError(f"{self.provider} cannot shift pitch; set pitch_semitones: 0")

    def _request(
        self, request: SpeechRequest
    ) -> tuple[str, dict[str, str], dict[str, str], dict[str, object]]:
        raise NotImplementedError


class CartesiaTTS(_HttpTTS):
    """Cartesia ``POST /tts/bytes``, raw ``pcm_s16le`` at 16 kHz."""

    provider = "cartesia"
    key_env = "CARTESIA_API_KEY"
    rate_range = (0.6, 1.5)
    sample_rate = 16_000
    api_version: ClassVar[str] = "2026-08-14"

    @classmethod
    def default_model(cls) -> str:
        """Cartesia's current Sonic model."""
        return "sonic-3.6"

    def _request(
        self, request: SpeechRequest
    ) -> tuple[str, dict[str, str], dict[str, str], dict[str, object]]:
        headers = {"Authorization": f"Bearer {self._key}", "Cartesia-Version": self.api_version}
        body: dict[str, object] = {
            "model_id": self._model,
            "transcript": request.text,
            "voice": request.voice,
            "locale": request.language,
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": self.sample_rate,
            },
            "generation_config": {"speed": request.rate},
        }
        return "https://api.cartesia.ai/tts/bytes", headers, {}, body


class ElevenLabsTTS(_HttpTTS):
    """ElevenLabs ``POST /v1/text-to-speech/{voice_id}``, ``pcm_16000``."""

    provider = "elevenlabs"
    key_env = "ELEVENLABS_API_KEY"
    rate_range = (0.7, 1.2)
    sample_rate = 16_000

    @classmethod
    def default_model(cls) -> str:
        """ElevenLabs' multilingual model."""
        return "eleven_multilingual_v2"

    def _request(
        self, request: SpeechRequest
    ) -> tuple[str, dict[str, str], dict[str, str], dict[str, object]]:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{request.voice}"
        body: dict[str, object] = {
            "text": request.text,
            "model_id": self._model,
            "voice_settings": {"speed": request.rate},
        }
        return url, {"xi-api-key": self._key}, {"output_format": "pcm_16000"}, body


class OpenAITTS(_HttpTTS):
    """OpenAI ``POST /v1/audio/speech``, ``pcm`` (24 kHz 16-bit)."""

    provider = "openai"
    key_env = "OPENAI_API_KEY"
    rate_range = (0.25, 4.0)
    sample_rate = 24_000

    @classmethod
    def default_model(cls) -> str:
        """OpenAI's small speech model."""
        return "gpt-4o-mini-tts"

    def _request(
        self, request: SpeechRequest
    ) -> tuple[str, dict[str, str], dict[str, str], dict[str, object]]:
        body: dict[str, object] = {
            "model": self._model,
            "input": request.text,
            "voice": request.voice,
            "response_format": "pcm",
            "speed": request.rate,
        }
        headers = {"Authorization": f"Bearer {self._key}"}
        return "https://api.openai.com/v1/audio/speech", headers, {}, body


CLOUD_PROVIDERS: Final[dict[str, type[_HttpTTS]]] = {
    cls.provider: cls for cls in (CartesiaTTS, ElevenLabsTTS, OpenAITTS)
}
