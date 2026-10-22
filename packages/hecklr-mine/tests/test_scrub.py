"""Stage 2 (WS13 scrub): PII becomes consistent placeholders and conversations stay coherent."""

import importlib.util
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import anyio
import pytest

from hecklr_llm import FakeLLM
from hecklr_mine.errors import MissingExtraError
from hecklr_mine.presidio import PresidioDetector
from hecklr_mine.scrub import (
    LiteralDetector,
    LlmPiiFinder,
    Placeholders,
    RegexDetector,
    Scrubber,
    iban_valid,
    luhn_valid,
    scrub_conversations,
)
from hecklr_schemas import Conversation, LLMRequest


def _conversation(cid: str, *turns: tuple[str, str], **extra: Any) -> Conversation:
    return Conversation.model_validate(
        {"id": cid, "turns": [{"role": r, "text": t} for r, t in turns], **extra}
    )


def _scrub(text: str) -> str:
    return Scrubber().scrub_text(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail me at Jane.Doe+x@example.co.uk now", "mail me at [EMAIL_1] now"),
        ("call +33 6 12 34 56 78 please", "call [PHONE_1] please"),
        ("my number is (415) 555-0134", "my number is [PHONE_1]"),
        ("card 4111 1111 1111 1111 expires", "card [CARD_1] expires"),
        ("IBAN FR76 3000 6000 0112 3456 7890 189 OK", "IBAN [IBAN_1] OK"),
        (
            "I live at 221B Baker Street, London NW1 6XE",
            "I live at [ADDRESS_1], London [POSTCODE_1]",
        ),
        ("c'est au 12 rue de la Paix", "c'est au [ADDRESS_1]"),
        ("my postcode is 92000", "my postcode is [POSTCODE_1]"),
        ("zip: 94105-1234", "zip: [POSTCODE_1]"),
    ],
)
def test_ws13_regex_rules_find_each_kind_of_pii(text: str, expected: str) -> None:
    assert _scrub(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "visit on 2026-10-23T09:00 for order 55210",
        "card 4111 1111 1111 1112 fails Luhn",  # also not a phone: 16 digits
        "IBAN FR76 3000 6000 0112 3456 7890 180",  # bad checksum
        "customer C-1042 pays 12000 euros",
        "invoice INV-2026-0042",
    ],
)
def test_ws13_regex_rules_leave_ordinary_numbers_alone(text: str) -> None:
    assert _scrub(text) == text


def test_ws13_checksums() -> None:
    assert luhn_valid("4111111111111111")
    assert not luhn_valid("4111111111111112")
    assert iban_valid("GB82 WEST 1234 5698 7654 32")
    assert not iban_valid("GB82")


def test_ws13_same_entity_same_placeholder_within_and_across_conversations() -> None:
    first = _conversation(
        "c1",
        ("user", "I'm jane@example.com, phone 06 12 34 56 78."),
        ("agent", "Thanks, I've noted JANE@example.com and 0612345678."),
        ("user", "Also bill bob@example.com"),
    )
    second = _conversation("c2", ("user", "This is jane@example.com again"))
    scrubber = Scrubber()
    one, two = scrubber.scrub(first), scrubber.scrub(second)
    assert [t.text for t in one.turns] == [
        "I'm [EMAIL_1], phone [PHONE_1].",
        "Thanks, I've noted [EMAIL_1] and [PHONE_1].",
        "Also bill [EMAIL_2]",
    ]
    assert two.turns[0].text == "This is [EMAIL_1] again"
    assert scrubber.placeholders.counts == {"EMAIL": 2, "PHONE": 1}


def test_ws13_scrubbing_keeps_the_conversation_coherent() -> None:
    """Structure, roles, times and cross-references survive; only the PII values change."""
    conversation = Conversation.model_validate(
        {
            "id": "c-coherent",
            "turns": [
                {"role": "user", "t_ms": 0, "text": "Send it to 12 High Street please."},
                {
                    "role": "agent",
                    "t_ms": 900,
                    "text": "Booked at 12 High Street.",
                    "tool_calls": [
                        {
                            "tool": "book",
                            "args": {"address": "12 High Street", "slots": [1, 2]},
                            "result": {"confirmation": "sent to amy@example.org"},
                            "error": "retry for amy@example.org",
                        }
                    ],
                },
                {"role": "user", "t_ms": 2000, "text": "No! My home is 40 Mill Lane."},
            ],
            "metadata": {"contact": "amy@example.org", "csat": 1},
        }
    )
    scrubbed = Scrubber().scrub(conversation)
    assert [(t.role, t.t_ms) for t in scrubbed.turns] == [
        (t.role, t.t_ms) for t in conversation.turns
    ]
    call = scrubbed.turns[1].tool_calls[0]
    assert call.args == {"address": "[ADDRESS_1]", "slots": [1, 2]}
    assert call.result == {"confirmation": "sent to [EMAIL_1]"}
    assert call.error == "retry for [EMAIL_1]"
    assert scrubbed.turns[0].text == "Send it to [ADDRESS_1] please."
    assert scrubbed.turns[1].text == "Booked at [ADDRESS_1]."
    assert scrubbed.turns[2].text == "No! My home is [ADDRESS_2]."
    assert scrubbed.metadata == {"contact": "[EMAIL_1]", "csat": 1}
    # Scrubbing is idempotent: placeholders are never scrubbed again.
    assert Scrubber().scrub(scrubbed).turns == scrubbed.turns


def test_ws13_overlapping_findings_keep_the_longest_earliest_span() -> None:
    literal = LiteralDetector({"Jane Doe": "PERSON", "Doe": "PERSON", "[EMAIL_1]": "EMAIL"})
    scrubber = Scrubber(detectors=[RegexDetector(), literal])
    assert scrubber.scrub_text("Jane Doe, Doe and [EMAIL_1]") == (
        "[PERSON_1], [PERSON_2] and [EMAIL_1]"
    )


def test_ws13_llm_pass_scrubs_names_after_the_regex_pass() -> None:
    conversations = [
        _conversation("c1", ("user", "I'm Marie Curie, marie@example.fr"), ("agent", "Hi Marie")),
        _conversation("c2", ("user", "Marie Curie here again")),
    ]
    seen: list[str] = []

    def responder(request: LLMRequest) -> dict[str, Any]:
        text = request.messages[-1].content
        seen.append(text)
        entities = [{"entity": "person", "text": "Marie Curie"}]
        if "Hi Marie" in text:
            entities.append({"entity": "PERSON", "text": "Marie"})
        entities.append({"entity": "PERSON", "text": "not in the text"})
        return {"entities": entities}

    finder = LlmPiiFinder(FakeLLM(responder=responder), "fake/pii", seed=3)
    scrubbed = anyio.run(
        lambda: scrub_conversations(conversations, Scrubber(), llm_finder=finder, concurrency=2)
    )
    assert [t.text for c in scrubbed for t in c.turns] == [
        "I'm [PERSON_1], [EMAIL_1]",
        "Hi [PERSON_2]",
        "[PERSON_1] here again",
    ]
    # The model only ever saw pattern-scrubbed text.
    assert all("marie@example.fr" not in text for text in seen)


def test_ws13_scrub_without_llm_is_synchronous_order() -> None:
    conversations = [_conversation("a", ("user", "x@y.io")), _conversation("b", ("user", "z@y.io"))]
    scrubbed = anyio.run(lambda: scrub_conversations(conversations, Scrubber()))
    assert [c.turns[0].text for c in scrubbed] == ["[EMAIL_1]", "[EMAIL_2]"]


def test_ws13_placeholder_identity_ignores_formatting() -> None:
    placeholders = Placeholders()
    assert placeholders.for_value("IBAN", "gb82 west 1234") == placeholders.for_value(
        "IBAN", "GB82WEST1234"
    )
    assert placeholders.for_value("PERSON", "Ann  Lee") == placeholders.for_value(
        "PERSON", "ann lee"
    )


# --- Presidio ------------------------------------------------------------------------------


@dataclass
class _Result:
    entity_type: str
    start: int
    end: int


class _FakeAnalyzer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def analyze(
        self, text: str, language: str, entities: list[str], score_threshold: float
    ) -> Sequence[_Result]:
        self.calls.append({"language": language, "threshold": score_threshold})
        start = text.index("Ada")
        return [_Result("PERSON", start, start + 3), _Result("DATE_TIME", 0, 3)]


def test_ws13_presidio_findings_are_mapped_and_filtered() -> None:
    analyzer = _FakeAnalyzer()
    scrubber = Scrubber(detectors=[RegexDetector(), PresidioDetector(analyzer, language="fr")])
    assert scrubber.scrub_text("Mon Ada called") == "Mon [PERSON_1] called"
    assert analyzer.calls == [{"language": "fr", "threshold": 0.5}]


def test_ws13_presidio_without_the_spacy_model_fails_with_instructions() -> None:
    with pytest.raises(MissingExtraError, match="spacy download no_such_model"):
        PresidioDetector.from_spacy_model("no_such_model")


@pytest.mark.skipif(
    importlib.util.find_spec("en_core_web_sm") is None,
    reason="spaCy model en_core_web_sm not installed (python -m spacy download en_core_web_sm)",
)
def test_ws13_presidio_finds_person_names_with_a_real_model() -> None:
    scrubber = Scrubber(detectors=[RegexDetector(), PresidioDetector.from_spacy_model()])
    scrubbed = scrubber.scrub_text("Hello, my name is Rafael Nadal and my email is r@n.es")
    assert "Rafael Nadal" not in scrubbed
    assert "[EMAIL_1]" in scrubbed
    json.dumps(scrubbed)
