"""Speech providers: synthetic TTS and its cache, cloud TTS over mocked HTTP, ASR, WER, VAD."""

import importlib.util
import json
import time
from collections.abc import Callable
from pathlib import Path

import anyio
import httpx2
import numpy as np
import pytest

from hecklr_voice.asr import (
    REFERENCE_SENTENCES,
    AsrSample,
    DeepgramASR,
    FakeASR,
    Transcript,
    measure_asr,
    reference_samples,
    wer,
    words,
)
from hecklr_voice.audio import (
    FRAME_SAMPLES,
    SAMPLE_RATE,
    pcm_bytes,
    to_pcm16,
)
from hecklr_voice.errors import VoiceConfigError, VoiceProviderError
from hecklr_voice.impair import ImpairmentChain, Segment, render_clip
from hecklr_voice.models import ensure_model_file, require_extra
from hecklr_voice.pool import run_dsp, warm_dsp_pool
from hecklr_voice.providers import make_asr, make_tts
from hecklr_voice.tts import CachedTTS, SpeechRequest, SyntheticTTS, voice_f0
from hecklr_voice.tts_cloud import CartesiaTTS, ElevenLabsTTS, OpenAITTS
from hecklr_voice.vad import EnergyVad, SileroVad

pytestmark = pytest.mark.anyio

Handler = Callable[[httpx2.Request], httpx2.Response]


def _pcm_reply(seconds: float, rate: int) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return pcm_bytes((0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32))


def _listing(directory: Path) -> list[Path]:
    """Directory contents; a sync helper so async tests do not block on the filesystem inline."""
    return sorted(directory.iterdir())


class Recorder:
    """A mock transport handler that records requests and replies with ``response``."""

    def __init__(self, response: httpx2.Response) -> None:
        self.response = response
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.response


async def test_synthetic_tts_is_deterministic_and_tagged() -> None:
    tts = SyntheticTTS()
    request = SpeechRequest(text="Your visit is on Tuesday.", voice="clerk")
    first = await tts.synthesize(request)
    assert np.array_equal(first, await tts.synthesize(request))
    assert first.dtype == np.float32
    heard = await FakeASR().transcribe(to_pcm16(first), language="en-US")
    assert heard == Transcript("Your visit is on Tuesday.", 1.0)
    untagged = await SyntheticTTS(tag=False).synthesize(request)
    assert (await FakeASR().transcribe(to_pcm16(untagged), language="en-US")).text == ""


async def test_synthetic_tts_voice_rate_and_pitch_change_the_audio() -> None:
    tts = SyntheticTTS()
    base = await tts.synthesize(SpeechRequest(text="one two three four", voice="a"))
    fast = await tts.synthesize(SpeechRequest(text="one two three four", voice="a", rate=2.0))
    higher = await tts.synthesize(
        SpeechRequest(text="one two three four", voice="a", pitch_semitones=5)
    )
    assert len(fast) < 0.7 * len(base)
    assert not np.array_equal(higher, base)
    assert 95 <= voice_f0("anyone") <= 230
    assert voice_f0("a") != voice_f0("b")


async def test_cached_tts_synthesizes_once_per_key(tmp_path: Path) -> None:
    class Counting:
        name = "counting"

        def __init__(self) -> None:
            self.calls = 0

        async def synthesize(self, request: SpeechRequest) -> np.ndarray:
            self.calls += 1
            return np.full(10, len(request.text), dtype=np.float32)

    inner = Counting()
    cached = CachedTTS(inner, tmp_path)
    request = SpeechRequest(text="hello", voice="v")
    first = await cached.synthesize(request)
    again = await cached.synthesize(request)
    other = await cached.synthesize(SpeechRequest(text="hello", voice="v", rate=1.1))
    assert inner.calls == 2
    assert np.array_equal(first, again)
    assert other is not None
    assert cached.name == "counting"
    assert cached.path_for(request) != cached.path_for(SpeechRequest(text="hello", voice="w"))
    assert len(_listing(tmp_path)) == 2


@pytest.mark.parametrize(
    ("cls", "key", "url_part", "rate"),
    [
        (CartesiaTTS, "CARTESIA_API_KEY", "api.cartesia.ai/tts/bytes", 16_000),
        (ElevenLabsTTS, "ELEVENLABS_API_KEY", "/v1/text-to-speech/voice-1", 16_000),
        (OpenAITTS, "OPENAI_API_KEY", "/v1/audio/speech", 24_000),
    ],
)
async def test_cloud_tts_posts_text_and_decodes_pcm(
    cls: type[CartesiaTTS | ElevenLabsTTS | OpenAITTS], key: str, url_part: str, rate: int
) -> None:
    handler = Recorder(httpx2.Response(200, content=_pcm_reply(0.5, rate)))
    tts = cls.from_environ({key: "secret"}, transport=httpx2.MockTransport(handler))
    audio = await tts.synthesize(SpeechRequest(text="Hello", voice="voice-1", rate=1.1))
    assert len(audio) == pytest.approx(0.5 * SAMPLE_RATE, abs=2)
    [request] = handler.requests
    assert url_part in str(request.url)
    body = json.loads(request.content)
    assert "Hello" in json.dumps(body)
    assert "secret" in " ".join(request.headers.values())
    assert tts.name.startswith(f"{cls.provider}/")


async def test_cloud_tts_failures_and_bad_settings_are_loud() -> None:
    with pytest.raises(VoiceConfigError, match="CARTESIA_API_KEY"):
        CartesiaTTS.from_environ({})
    failing = Recorder(httpx2.Response(500, text="boom"))
    tts = OpenAITTS.from_environ({"OPENAI_API_KEY": "k"}, transport=httpx2.MockTransport(failing))
    with pytest.raises(VoiceProviderError, match="HTTP 500"):
        await tts.synthesize(SpeechRequest(text="hi", voice="alloy"))
    with pytest.raises(VoiceConfigError, match="rate"):
        await tts.synthesize(SpeechRequest(text="hi", voice="alloy", rate=5.0))
    with pytest.raises(VoiceConfigError, match="pitch"):
        await tts.synthesize(SpeechRequest(text="hi", voice="alloy", pitch_semitones=2))

    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    offline = OpenAITTS.from_environ(
        {"OPENAI_API_KEY": "k"}, transport=httpx2.MockTransport(unreachable)
    )
    with pytest.raises(VoiceProviderError, match="request failed"):
        await offline.synthesize(SpeechRequest(text="hi", voice="alloy"))


def test_make_tts_builds_each_provider(tmp_path: Path) -> None:
    assert isinstance(make_tts("synthetic"), SyntheticTTS)
    assert isinstance(make_tts("synthetic", cache_dir=tmp_path), CachedTTS)
    assert isinstance(make_tts("cartesia", environ={"CARTESIA_API_KEY": "k"}), CartesiaTTS)
    if importlib.util.find_spec("kokoro_onnx") is None:
        with pytest.raises(VoiceConfigError, match="extra"):
            make_tts("kokoro", models_dir=tmp_path)
    else:
        assert make_tts("kokoro", models_dir=tmp_path).name == "kokoro/v1.0"
    with pytest.raises(VoiceConfigError, match="unknown TTS provider"):
        make_tts("piperish")


def test_make_asr_builds_each_provider(tmp_path: Path) -> None:
    assert isinstance(make_asr("fake/tag"), FakeASR)
    if importlib.util.find_spec("faster_whisper") is None:
        with pytest.raises(VoiceConfigError, match="extra"):
            make_asr("faster-whisper/tiny", models_dir=tmp_path)
    else:
        assert make_asr("faster-whisper/tiny", models_dir=tmp_path).name == "faster-whisper/tiny"
    deepgram = make_asr("deepgram/nova-3", environ={"DEEPGRAM_API_KEY": "k"})
    assert deepgram.name == "deepgram/nova-3"
    with pytest.raises(VoiceConfigError, match="provider/model"):
        make_asr("whisper")
    with pytest.raises(VoiceConfigError, match="unknown ASR provider"):
        make_asr("other/model")
    with pytest.raises(VoiceConfigError, match="DEEPGRAM_API_KEY"):
        make_asr("deepgram/nova-3", environ={})


def test_require_extra_names_the_missing_extra() -> None:
    with pytest.raises(VoiceConfigError, match=r"hecklr-voice\[local\]"):
        require_extra("hecklr_no_such_module", "a test provider")


async def test_deepgram_uploads_linear16_and_reads_the_best_alternative() -> None:
    body = {"results": {"channels": [{"alternatives": [{"transcript": "hi", "confidence": 0.9}]}]}}
    handler = Recorder(httpx2.Response(200, json=body))
    asr = DeepgramASR("k", transport=httpx2.MockTransport(handler))
    heard = await asr.transcribe(np.zeros(1600, dtype=np.int16), language="fr-FR")
    assert heard == Transcript("hi", 0.9)
    [request] = handler.requests
    assert request.url.params["encoding"] == "linear16"
    assert request.url.params["language"] == "fr-FR"
    assert request.headers["authorization"] == "Token k"
    assert len(request.content) == 3200


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx2.Response(401), "HTTP 401"),
        (httpx2.Response(200, json={"results": {}}), "unexpected body"),
    ],
)
async def test_deepgram_failures_are_provider_errors(
    response: httpx2.Response, message: str
) -> None:
    asr = DeepgramASR("k", transport=httpx2.MockTransport(Recorder(response)))
    with pytest.raises(VoiceProviderError, match=message):
        await asr.transcribe(np.zeros(10, dtype=np.int16), language="en-US")


@pytest.mark.parametrize(
    ("reference", "hypothesis", "expected"),
    [
        ("move my visit", "move my visit", 0.0),
        ("Move my visit.", "move my visit", 0.0),
        ("move my visit", "move visit", 1 / 3),
        ("move my visit", "move my old visit", 1 / 3),
        ("move my visit", "prove by visits", 1.0),
        ("", "", 0.0),
        ("", "noise", 1.0),
    ],
)
def test_wer_counts_substitutions_deletions_and_insertions(
    reference: str, hypothesis: str, expected: float
) -> None:
    assert wer(reference, hypothesis) == pytest.approx(expected)


def test_words_normalizes_case_and_punctuation() -> None:
    assert words("Hello, it's ME!") == ["hello", "it's", "me"]


async def test_measure_asr_reports_wer_with_an_interval() -> None:
    tts = SyntheticTTS()
    noisy_line = ImpairmentChain(environment="street", snr_db=10.0)
    noisy = await reference_samples(tts, noisy_line, voice="agent", seed=1)
    perfect = await measure_asr(FakeASR(), [_raw(sample) for sample in await _tagged(tts)], seed=3)
    assert perfect.wer.value == 0.0
    assert perfect.wer.ci_low <= perfect.wer.value <= perfect.wer.ci_high
    assert perfect.utterances == 8
    # Noise wipes the tag: FakeASR hears nothing at all.
    deaf = await measure_asr(FakeASR(), noisy, seed=3)
    assert deaf.wer.value == 1.0
    assert deaf.asr == "fake/tag"
    with pytest.raises(ValueError, match="at least one"):
        await measure_asr(FakeASR(), [])


async def _tagged(tts: SyntheticTTS) -> list[tuple[str, np.ndarray]]:
    return [
        (text, await tts.synthesize(SpeechRequest(text=text, voice="agent")))
        for text in REFERENCE_SENTENCES
    ]


def _raw(sample: tuple[str, np.ndarray]) -> AsrSample:
    text, audio = sample
    return AsrSample(to_pcm16(audio), text)


async def test_energy_vad_separates_speech_from_silence_and_faint_noise() -> None:
    vad = EnergyVad()
    speech = to_pcm16(await SyntheticTTS().synthesize(SpeechRequest(text="hello", voice="a")))
    frame = speech[FRAME_SAMPLES * 5 : FRAME_SAMPLES * 6]
    assert await vad.is_speech(frame)
    assert not await vad.is_speech(np.zeros(FRAME_SAMPLES, dtype=np.int16))
    faint = to_pcm16(
        (0.001 * np.random.default_rng(0).standard_normal(FRAME_SAMPLES)).astype(np.float32)
    )
    assert not await vad.is_speech(faint)
    assert await EnergyVad(threshold_dbfs=-70).is_speech(faint)


async def test_silero_vad_chains_state_and_context_over_512_sample_windows() -> None:
    class FakeSession:
        def __init__(self) -> None:
            self.inputs: list[dict[str, np.ndarray]] = []

        def run(self, _: None, inputs: dict[str, np.ndarray]) -> list[np.ndarray]:
            self.inputs.append(inputs)
            loud = float(np.abs(inputs["input"]).max()) > 0.1
            return [np.array([[0.9 if loud else 0.1]]), inputs["state"] + 1]

    session = FakeSession()
    vad = SileroVad(session=session)
    loud = to_pcm16(np.full(FRAME_SAMPLES, 0.5, dtype=np.float32))
    assert not await vad.is_speech(loud)  # 320 samples: no complete window yet
    assert await vad.is_speech(loud)  # 640 samples: one window scored
    silence = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    verdicts = [await vad.is_speech(silence) for _ in range(4)]
    assert verdicts == [True, True, False, False]  # the decision lags by up to one window
    first, second, *_ = session.inputs
    assert first["input"].shape == (1, 576)
    assert int(first["sr"]) == SAMPLE_RATE
    assert np.array_equal(second["state"], first["state"] + 1)
    assert np.array_equal(second["input"][:, :64], first["input"][:, -64:])


async def test_ensure_model_file_downloads_once_atomically(tmp_path: Path) -> None:
    calls: list[str] = []

    def serve(request: httpx2.Request) -> httpx2.Response:
        calls.append(str(request.url))
        return httpx2.Response(200, content=b"model-bytes")

    target = tmp_path / "m" / "model.onnx"
    transport = httpx2.MockTransport(serve)
    assert await ensure_model_file("https://x.test/m", target, transport=transport) == target
    await ensure_model_file("https://x.test/m", target, transport=transport)
    assert target.read_bytes() == b"model-bytes"
    assert calls == ["https://x.test/m"]
    assert _listing(target.parent) == [target]


async def test_ensure_model_file_refuses_bad_downloads(tmp_path: Path) -> None:
    good = httpx2.MockTransport(lambda request: httpx2.Response(200, content=b"x"))
    with pytest.raises(VoiceProviderError, match="checksum"):
        await ensure_model_file("https://x.test/m", tmp_path / "a", sha256="0" * 64, transport=good)
    missing = httpx2.MockTransport(lambda request: httpx2.Response(404))
    with pytest.raises(VoiceProviderError, match="HTTP 404"):
        await ensure_model_file("https://x.test/m", tmp_path / "b", transport=missing)

    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    with pytest.raises(VoiceProviderError, match="refused"):
        await ensure_model_file(
            "https://x.test/m", tmp_path / "c", transport=httpx2.MockTransport(unreachable)
        )
    assert _listing(tmp_path) == []


@pytest.mark.usefixtures("frozen_heap")
async def test_dsp_pool_never_blocks_the_event_loop() -> None:
    """Rendering eight noisy 5 s clips at once must leave the loop free for 20 ms frames.

    The loop thread's own CPU time between heartbeats is measured, not wall-clock lag: the
    claim is that no DSP runs on the loop, and CPU time stays exact even when other test
    processes keep the machine busy.
    """
    speech = np.full(5 * SAMPLE_RATE, 0.1, dtype=np.float32)
    chain = ImpairmentChain(environment="cafe", snr_db=5.0, reverb="hall")
    await warm_dsp_pool()
    worst_busy_ms = 0.0
    beats = 0
    done = False

    async def heartbeat() -> None:
        nonlocal worst_busy_ms, beats
        while not done:
            started = time.thread_time()
            await anyio.sleep(0.005)
            worst_busy_ms = max(worst_busy_ms, (time.thread_time() - started) * 1000)
            beats += 1

    async with anyio.create_task_group() as group:
        group.start_soon(heartbeat)
        async with anyio.create_task_group() as jobs:
            for seed in range(8):
                jobs.start_soon(run_dsp, render_clip, (Segment("speech", speech),), chain, seed)
        done = True
    assert beats > 20
    assert worst_busy_ms < 5.0
