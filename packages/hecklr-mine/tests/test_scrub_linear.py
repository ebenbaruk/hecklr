"""Issue #116: the scrubber is linear in its input, and finds exactly the e-mails it always did."""

import random
import re
import time

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hecklr_mine.scrub import RegexDetector, Scrubber

# The e-mail rule before #116, kept as the oracle of what the linear finder must match: it was
# correct, only quadratic on long runs of local-part characters with no "@" after them.
REFERENCE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
MEGABYTE = 1_000_000
# A 1 MB turn took the old rule about 15 minutes; the linear scrubber needs a fraction of a
# second, so the bound leaves room for a loaded CI host while failing any quadratic rule by far.
MAX_SECONDS = 1.0
_rng = random.Random(116)
LONG_TURNS = {
    "hex dump": _rng.randbytes(MEGABYTE // 2).hex(),
    "alphanumeric token": "".join(
        _rng.choice("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")
        for _ in range(MEGABYTE)
    ),
    "digits": "".join(_rng.choice("0123456789") for _ in range(MEGABYTE)),
    "dotted labels": "a." * (MEGABYTE // 2),
    "at signs": "ab@" * (MEGABYTE // 3),
    "domain with no tld": "x@" + "a1." * (MEGABYTE // 3),
    "local part then an e-mail": "a" * MEGABYTE + "@example.com",
}


@pytest.mark.parametrize("text", LONG_TURNS.values(), ids=LONG_TURNS.keys())
def test_issue116_a_1mb_turn_scrubs_in_well_under_a_second(text: str) -> None:
    started = time.perf_counter()
    Scrubber().scrub_text(text)
    assert time.perf_counter() - started < MAX_SECONDS


def test_issue116_a_long_local_part_is_still_one_email() -> None:
    text = "token " + "a" * MEGABYTE + "@example.com ok"
    assert Scrubber().scrub_text(text) == "token [EMAIL_1] ok"


def _email_spans(text: str) -> list[tuple[int, int]]:
    return [(s.start, s.end) for s in RegexDetector().find(text) if s.entity == "EMAIL"]


@settings(max_examples=2000)
@given(st.text(alphabet="ab1.-_%+@ xX\n", max_size=60))
def test_issue116_email_spans_match_the_reference_rule(text: str) -> None:
    assert _email_spans(text) == [(m.start(), m.end()) for m in REFERENCE_EMAIL.finditer(text)]


@settings(max_examples=500)
@given(st.text(max_size=200))
def test_issue116_email_spans_match_the_reference_rule_on_any_text(text: str) -> None:
    assert _email_spans(text) == [(m.start(), m.end()) for m in REFERENCE_EMAIL.finditer(text)]


@settings(max_examples=300)
@given(st.text(alphabet=st.characters(exclude_characters="@"), max_size=200))
def test_issue116_text_without_an_at_sign_is_unchanged(text: str) -> None:
    scrubbed = Scrubber().scrub_text(text)
    assert _email_spans(text) == []
    assert "[EMAIL_" not in scrubbed
