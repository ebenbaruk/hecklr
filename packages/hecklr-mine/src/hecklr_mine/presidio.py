"""Presidio as a PII detector: names and locations that regex rules cannot find.

Optional: install ``hecklr-mine[presidio]`` plus a spaCy model (``python -m spacy download
en_core_web_sm``). Presidio downloads a missing model on first use, a surprise network call in the
middle of a mining run, so ``from_spacy_model`` refuses to start without the model instead.
"""

import importlib
import importlib.util
from collections.abc import Sequence
from typing import Final, Protocol

from hecklr_mine.errors import MissingExtraError
from hecklr_mine.scrub import PiiSpan

ENTITY_NAMES: Final = {
    "PERSON": "PERSON",
    "LOCATION": "LOCATION",
    "PHONE_NUMBER": "PHONE",
    "CREDIT_CARD": "CARD",
    "IBAN_CODE": "IBAN",
    "IP_ADDRESS": "IP",
    "US_SSN": "SSN",
    "US_BANK_NUMBER": "ACCOUNT",
    "US_DRIVER_LICENSE": "LICENSE",
    "US_PASSPORT": "PASSPORT",
    "UK_NHS": "NHS",
}
"""Presidio entity -> Hecklr placeholder kind. Dates and URLs are left alone on purpose: a
scenario needs "Thursday 9am" and the company's web address to stay meaningful. E-mail is left to
the regex rules: Presidio's e-mail recognizer downloads the public-suffix list (tldextract) on
first use, a network call no mining run should make."""


class AnalyzerResult(Protocol):
    """The fields of a Presidio ``RecognizerResult`` the detector reads."""

    entity_type: str
    start: int
    end: int


class Analyzer(Protocol):
    """The part of Presidio's ``AnalyzerEngine`` the detector calls."""

    def analyze(
        self, text: str, language: str, entities: list[str], score_threshold: float
    ) -> Sequence[AnalyzerResult]:
        """Find entities in ``text``."""
        ...


class PresidioDetector:
    """Wraps a Presidio analyzer as a ``PiiDetector``."""

    def __init__(
        self, analyzer: Analyzer, *, language: str = "en", score_threshold: float = 0.5
    ) -> None:
        self._analyzer = analyzer
        self._language = language
        self._threshold = score_threshold

    @classmethod
    def from_spacy_model(
        cls, model: str = "en_core_web_sm", language: str = "en"
    ) -> "PresidioDetector":
        """A detector over a local spaCy model.

        Raises:
            MissingExtraError: Presidio or the spaCy model is not installed.
        """
        try:
            analyzer_module = importlib.import_module("presidio_analyzer")
            nlp_module = importlib.import_module("presidio_analyzer.nlp_engine")
        except ImportError as exc:
            raise MissingExtraError(
                "PII scrubbing with Presidio needs the extra: pip install 'hecklr-mine[presidio]'"
            ) from exc
        if importlib.util.find_spec(model) is None:
            raise MissingExtraError(
                f"Presidio needs the spaCy model {model!r}: python -m spacy download {model}"
            )
        provider = nlp_module.NlpEngineProvider(
            nlp_configuration={
                "nlp_engine_name": "spacy",
                "models": [{"lang_code": language, "model_name": model}],
            }
        )
        analyzer: Analyzer = analyzer_module.AnalyzerEngine(
            nlp_engine=provider.create_engine(), supported_languages=[language]
        )
        return cls(analyzer, language=language)

    def find(self, text: str) -> list[PiiSpan]:
        """Presidio's findings above the score threshold, in Hecklr's entity names."""
        results = self._analyzer.analyze(
            text=text,
            language=self._language,
            entities=list(ENTITY_NAMES),
            score_threshold=self._threshold,
        )
        return [
            PiiSpan(result.start, result.end, ENTITY_NAMES[result.entity_type])
            for result in results
            if result.entity_type in ENTITY_NAMES
        ]
