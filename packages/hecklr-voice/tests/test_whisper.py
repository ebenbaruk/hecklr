"""faster-whisper as Hecklr hears the agent: words only, without timestamp tokens (#48).

Hecklr needs the words of an agent turn and a confidence; the turn's edges come from the audio
(ADR 0009a). Timestamped decoding cost 5.8-9 s instead of 0.4 s on some 5-7 s utterances, every
run, with the same words (issue #48, measured on captured Kokoro audio).
"""

import importlib
import importlib.util
import shutil
import subprocess
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from hecklr_voice import asr
from hecklr_voice.asr import Transcript, wer, whisper_transcribe
from hecklr_voice.audio import SAMPLE_RATE, Pcm16


@dataclass
class Segment:
    text: str
    avg_logprob: float


class FakeWhisper:
    def __init__(self, segments: list[Segment]) -> None:
        self.segments = segments
        self.calls: list[dict[str, object]] = []

    def transcribe(self, audio: object, **options: object) -> tuple[list[Segment], None]:
        self.calls.append(options)
        return self.segments, None


def test_issue48_whisper_decodes_words_without_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    whisper = FakeWhisper([Segment(" My customer number", -0.1), Segment(" is C1609. ", -0.3)])
    monkeypatch.setattr(asr, "_whisper", lambda model, root: whisper)

    heard = whisper_transcribe("base.en", "/models", np.zeros(1600, dtype=np.int16), "en-GB")

    assert heard.text == "My customer number is C1609."
    assert heard.confidence == pytest.approx(np.exp(-0.2))
    assert whisper.calls == [{"language": "en", "beam_size": 1, "without_timestamps": True}]


def test_whisper_hearing_nothing_has_zero_confidence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(asr, "_whisper", lambda model, root: FakeWhisper([]))
    silence = np.zeros(1600, dtype=np.int16)
    assert whisper_transcribe("base.en", "/m", silence, "en-US") == Transcript("", 0.0)


# --- the real base.en model, offline -----------------------------------------------------------

CALLER_LINES = (
    "My customer number is C1609, and my postcode is 92300.",
    "My customer number is C1042 and my postcode is 92000.",
    "I need to book a boiler service for next Thursday morning, please.",
    "Hello, I would like to cancel my appointment for tomorrow afternoon.",
)
"""Caller-turn shapes of the acme voice suite: 4-7 s, customer ids and postcodes."""


VOICE = "Samantha"
"""A US English voice; the default system voice may speak another accent or language."""


def _local_base_en() -> str:
    """The base.en model from the Hugging Face cache, without touching the network."""
    if importlib.util.find_spec("faster_whisper") is None:
        pytest.skip("needs the 'local' extra")
    utils = importlib.import_module("faster_whisper.utils")  # untyped optional extra
    try:
        return str(utils.download_model("base.en", local_files_only=True))
    except Exception as exc:  # noqa: BLE001 -- any cache miss means the model is not on disk
        pytest.skip(f"faster-whisper base.en is not in the local Hugging Face cache: {exc!r}")


def _spoken(text: str, directory: Path) -> Pcm16:
    """``text`` spoken by macOS's synthesizer in its US English voice, 16 kHz mono."""
    say = shutil.which("say")
    if say is None:
        pytest.skip("needs a local speech synthesizer (macOS say)")
    voices = subprocess.run([say, "-v", "?"], capture_output=True, text=True, check=True).stdout  # noqa: S603 -- the system synthesizer, fixed arguments
    if not any(line.startswith(f"{VOICE} ") for line in voices.splitlines()):
        pytest.skip(f"the {VOICE} voice is not installed")
    path = directory / "line.wav"
    subprocess.run(  # noqa: S603 -- the system synthesizer with fixed arguments
        [say, "-v", VOICE, "-o", str(path), f"--data-format=LEI16@{SAMPLE_RATE}", text],
        check=True,
    )
    with wave.open(str(path)) as audio:
        return np.frombuffer(audio.readframes(audio.getnframes()), dtype=np.int16)


@pytest.mark.slow
def test_issue48_words_only_decode_is_fast_and_as_accurate_on_caller_turns(
    tmp_path: Path,
) -> None:
    model = _local_base_en()
    whisper: Any = asr._whisper(model, str(tmp_path))
    errors: list[float] = []
    timestamped_errors: list[float] = []
    for line in CALLER_LINES:
        pcm = _spoken(line, tmp_path)
        started = time.process_time()
        heard = whisper_transcribe(model, str(tmp_path), pcm, "en-US")
        cpu_s = time.process_time() - started
        segments, _ = whisper.transcribe(
            pcm.astype(np.float32) / 32768, language="en", beam_size=1, without_timestamps=False
        )
        timestamped = " ".join(segment.text.strip() for segment in segments)
        errors.append(wer(line, heard.text))
        timestamped_errors.append(wer(line, timestamped))
        print(f"{len(pcm) / SAMPLE_RATE:.1f} s of speech, {cpu_s:.2f} s CPU: {heard.text}")
        # Generous: ~0.4 s of CPU on a 6 s line idle; the timestamped decode took 6-9 s (#48).
        assert cpu_s < len(pcm) / SAMPLE_RATE / 2, (line, cpu_s)
        assert heard.confidence is not None
        assert heard.confidence > 0.5
    assert sum(errors) <= sum(timestamped_errors), (errors, timestamped_errors)
    assert np.mean(errors) < 0.15, errors
