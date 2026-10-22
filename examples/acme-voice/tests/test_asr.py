"""The agent's recognizer: words-only faster-whisper, other providers from hecklr-voice."""

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pytest
from acme_voice import asr
from acme_voice.asr import WordsASR, make_agent_asr, transcribe_words

from hecklr_voice import FakeASR


@dataclass
class Segment:
    text: str


class FakeWhisper:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def transcribe(self, audio: object, **options: object) -> tuple[list[Segment], None]:
        self.calls.append(options)
        return [Segment(" My customer number"), Segment(" is C1609. ")], None


def test_words_are_decoded_without_timestamps(monkeypatch: pytest.MonkeyPatch) -> None:
    whisper = FakeWhisper()
    monkeypatch.setattr(asr, "_model", lambda model, root: whisper)
    heard = transcribe_words("base.en", "/models", np.zeros(1600, dtype=np.int16), "en-GB")
    assert heard.text == "My customer number is C1609."
    assert whisper.calls == [{"language": "en", "beam_size": 1, "without_timestamps": True}]


def test_whisper_names_get_the_words_only_recognizer(tmp_path: Path) -> None:
    recognizer = make_agent_asr("faster-whisper/base.en", models_dir=tmp_path)
    assert isinstance(recognizer, WordsASR)
    assert recognizer.name == "faster-whisper/base.en"
    assert isinstance(make_agent_asr("fake/tag", models_dir=tmp_path), FakeASR)
