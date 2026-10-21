"""Speech recognition of the agent's audio, and how good Hecklr's own recognition is.

The simulated caller only "hears" the agent through ASR, so ASR mistakes would look like agent
mistakes. :func:`measure_asr` scores an ASR on a reference set (word error rate with a bootstrap
interval) so reports can show the harness's own error next to the agent's (spec, "Inbound path").
Providers: ``fake`` (reads ``SyntheticTTS`` tags), ``faster-whisper`` (local, extra ``local``),
``deepgram`` (HTTP, key-gated).
"""

import importlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

import httpx2
import numpy as np

from hecklr_schemas import Estimate, IntervalMethod
from hecklr_voice.audio import (
    SAMPLE_RATE,
    Pcm16,
    bytes_to_pcm16,
    from_pcm16,
    stream_rng,
    stream_seed,
)
from hecklr_voice.errors import VoiceConfigError, VoiceProviderError
from hecklr_voice.impair import ImpairmentChain, Segment, render_clip
from hecklr_voice.models import DEFAULT_MODELS_DIR, MODEL_THREADS, require_extra
from hecklr_voice.pool import run_dsp
from hecklr_voice.tag import extract_tag
from hecklr_voice.tts import TTS, SpeechRequest

_WORD = re.compile(r"[^\W_]+(?:'[^\W_]+)*")


@dataclass(frozen=True, slots=True)
class Transcript:
    """What the ASR heard."""

    text: str
    confidence: float | None = None
    """0 to 1 when the provider reports it."""


@runtime_checkable
class ASR(Protocol):
    """A speech recognizer for 16 kHz mono 16-bit audio."""

    @property
    def name(self) -> str:
        """Provider and model, e.g. ``faster-whisper/small``."""
        ...

    async def transcribe(self, pcm: Pcm16, *, language: str) -> Transcript:
        """Transcribe one stretch of agent speech; ``language`` is a BCP 47 tag."""
        ...


class FakeASR:
    """Exact transcripts of ``SyntheticTTS`` audio received over a clean link, from its tag.

    Audio without an intact tag (real speech, or anything impaired) transcribes as empty text
    with confidence 0, which is what a recognizer reports for audio it cannot make out.
    """

    @property
    def name(self) -> str:
        """``fake/tag``."""
        return "fake/tag"

    async def transcribe(self, pcm: Pcm16, *, language: str) -> Transcript:
        """Decode the LSB tag in a worker process."""
        text = await run_dsp(extract_tag, pcm)
        return Transcript(text, 1.0) if text is not None else Transcript("", 0.0)


@lru_cache(maxsize=2)
def _whisper(model: str, download_root: str) -> Any:
    # One model load per worker process, reused for every utterance it transcribes.
    module = importlib.import_module("faster_whisper")
    return module.WhisperModel(
        model,
        device="cpu",
        compute_type="int8",
        cpu_threads=MODEL_THREADS,
        download_root=download_root,
    )


def whisper_transcribe(model: str, download_root: str, pcm: Pcm16, language: str) -> Transcript:
    """Pool job: transcribe with faster-whisper; confidence is the mean token probability."""
    segments, _ = _whisper(model, download_root).transcribe(
        from_pcm16(pcm), language=language.split("-")[0].lower(), beam_size=1
    )
    parts = list(segments)
    text = " ".join(part.text.strip() for part in parts).strip()
    if not parts:
        return Transcript(text, 0.0)
    confidence = float(np.exp(np.mean([part.avg_logprob for part in parts])))
    return Transcript(text, min(1.0, confidence))


class WhisperASR:
    """faster-whisper on CPU (int8); the model downloads into ``models_dir`` on first use."""

    def __init__(self, model: str = "small", *, models_dir: Path = DEFAULT_MODELS_DIR) -> None:
        require_extra("faster_whisper", "the faster-whisper ASR provider")
        self._model = model
        self._root = str(models_dir / "whisper")

    @property
    def name(self) -> str:
        """``faster-whisper/<model>``."""
        return f"faster-whisper/{self._model}"

    async def transcribe(self, pcm: Pcm16, *, language: str) -> Transcript:
        """Transcribe in a worker process."""
        return await run_dsp(whisper_transcribe, self._model, self._root, pcm, language)


class DeepgramASR:
    """Deepgram pre-recorded ``POST /v1/listen`` with raw ``linear16`` audio."""

    key_env: Final = "DEEPGRAM_API_KEY"

    def __init__(
        self,
        api_key: str,
        model: str = "nova-3",
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._key = api_key
        self._model = model
        self._transport = transport

    @classmethod
    def from_environ(
        cls,
        environ: Mapping[str, str],
        model: str = "nova-3",
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> "DeepgramASR":
        """Build from ``DEEPGRAM_API_KEY``; refuse to start without it."""
        key = environ.get(cls.key_env)
        if not key:
            raise VoiceConfigError(f"the deepgram ASR provider needs {cls.key_env} to be set")
        return cls(key, model, transport=transport)

    @property
    def name(self) -> str:
        """``deepgram/<model>``."""
        return f"deepgram/{self._model}"

    async def transcribe(self, pcm: Pcm16, *, language: str) -> Transcript:
        """Upload the audio and read the first alternative of the first channel."""
        params = {
            "model": self._model,
            "language": language,
            "encoding": "linear16",
            "sample_rate": str(SAMPLE_RATE),
            "channels": "1",
        }
        headers = {"Authorization": f"Token {self._key}", "Content-Type": "audio/raw"}
        try:
            async with httpx2.AsyncClient(transport=self._transport, timeout=30.0) as client:
                response = await client.post(
                    "https://api.deepgram.com/v1/listen",
                    params=params,
                    headers=headers,
                    content=pcm.astype("<i2").tobytes(),
                )
        except httpx2.HTTPError as exc:
            raise VoiceProviderError(f"deepgram request failed: {exc}") from exc
        if response.status_code != 200:
            raise VoiceProviderError(f"deepgram returned HTTP {response.status_code}")
        try:
            best = response.json()["results"]["channels"][0]["alternatives"][0]
            return Transcript(str(best["transcript"]), float(best["confidence"]))
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise VoiceProviderError(f"deepgram returned an unexpected body: {exc}") from exc


# --- measuring Hecklr's own ASR --------------------------------------------------------------


def words(text: str) -> list[str]:
    """Normalized words for scoring: lower case, punctuation dropped."""
    return [word.casefold() for word in _WORD.findall(text)]


def edit_distance(reference: Sequence[str], hypothesis: Sequence[str]) -> int:
    """Word-level Levenshtein distance (substitutions, deletions, insertions)."""
    previous = list(range(len(hypothesis) + 1))
    for row, ref_word in enumerate(reference, start=1):
        current = [row]
        for col, hyp_word in enumerate(hypothesis, start=1):
            current.append(
                min(
                    previous[col] + 1,
                    current[col - 1] + 1,
                    previous[col - 1] + (ref_word != hyp_word),
                )
            )
        previous = current
    return previous[-1]


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate of one utterance; an empty reference has WER 0 only if nothing was heard."""
    ref, hyp = words(reference), words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_distance(ref, hyp) / len(ref)


@dataclass(frozen=True, slots=True)
class AsrSample:
    """One reference utterance: the audio and what was actually said."""

    pcm: Pcm16
    reference: str


@dataclass(frozen=True, slots=True)
class AsrQuality:
    """How well an ASR did on a reference set."""

    asr: str
    wer: Estimate
    """Corpus WER (total word errors over total reference words) with a bootstrap interval."""
    utterances: int


async def measure_asr(
    asr: ASR,
    samples: Sequence[AsrSample],
    *,
    language: str = "en-US",
    seed: int = 0,
    resamples: int = 2_000,
    confidence: float = 0.95,
) -> AsrQuality:
    """Transcribe every sample and report corpus WER with a seeded bootstrap interval."""
    if not samples:
        raise ValueError("measuring an ASR needs at least one reference sample")
    errors = np.zeros(len(samples))
    totals = np.zeros(len(samples))
    for index, sample in enumerate(samples):
        heard = await asr.transcribe(sample.pcm, language=language)
        ref = words(sample.reference)
        errors[index] = edit_distance(ref, words(heard.text))
        totals[index] = max(1, len(ref))
    picks = stream_rng(seed, "asr-bootstrap").integers(0, len(samples), (resamples, len(samples)))
    replicates = errors[picks].sum(axis=1) / totals[picks].sum(axis=1)
    tail = (1 - confidence) / 2
    low, high = np.quantile(replicates, [tail, 1 - tail])
    value = float(errors.sum() / totals.sum())
    estimate = Estimate(
        value=value,
        ci_low=min(value, float(low)),
        ci_high=max(value, float(high)),
        confidence=confidence,
        n=len(samples),
        method=IntervalMethod.BOOTSTRAP,
    )
    return AsrQuality(asr=asr.name, wer=estimate, utterances=len(samples))


REFERENCE_SENTENCES: Final = (
    "Thanks for calling Acme maintenance, how can I help you today?",
    "Your boiler visit is booked for Thursday at nine in the morning.",
    "Could you confirm your customer number and postcode please?",
    "I have moved the appointment to next Tuesday between eight and noon.",
    "There is no cancellation fee for changing your visit.",
    "If you smell gas, leave the house and call the emergency line now.",
    "I am transferring you to a colleague who can help with billing.",
    "Is there anything else I can help you with today?",
)
"""Agent-side sentences for the ASR reference set, in the reference agent's domain."""


async def reference_samples(
    tts: TTS,
    chain: ImpairmentChain,
    *,
    voice: str,
    seed: int,
    sentences: Sequence[str] = REFERENCE_SENTENCES,
) -> list[AsrSample]:
    """Speak ``sentences`` with ``tts`` through ``chain``: the set :func:`measure_asr` scores.

    Measuring once per impairment chain (e.g. per SNR bucket) shows how much of a degradation
    curve is Hecklr's own hearing rather than the agent's.
    """
    samples: list[AsrSample] = []
    for index, sentence in enumerate(sentences):
        audio = await tts.synthesize(SpeechRequest(text=sentence, voice=voice))
        segments = (Segment("speech", audio),)
        clip = await run_dsp(render_clip, segments, chain, stream_seed(seed, index))
        samples.append(AsrSample(bytes_to_pcm16(clip.pcm), sentence))
    return samples
