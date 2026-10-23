"""The rules brain's message understanding."""

from datetime import date, datetime

import pytest
from acme_agent.rules import nlu
from acme_agent.rules.behaviour import ISO_SLOT, slot_format

TODAY = date(2026, 10, 21)  # a Wednesday


def test_identifiers_are_normalized() -> None:
    text = "I'm c1042, invoice INV 20481 and visit A-3001, postcode 92000"
    assert nlu.customer_ids(text) == ["C-1042"]
    assert nlu.invoice_ids(text) == ["INV-20481"]
    assert nlu.appointment_ids(text) == ["A-3001"]
    assert nlu.postcode(text) == "92000"


def test_invoice_digits_are_not_a_postcode() -> None:
    assert nlu.postcode("It's invoice INV-20481") is None
    assert nlu.postcode("C-1042, 75011") == "75011"


@pytest.mark.parametrize(
    ("text", "answer"),
    [
        ("Yes please", True),
        ("ok that works", True),
        ("Perfect, go ahead", True),
        ("No, another one", False),
        ("that doesn't work for me", False),
        ("no problem, go ahead", True),
        ("C-1042", None),
    ],
)
def test_yes_no(text: str, answer: bool | None) -> None:
    assert nlu.yes_no(text) is answer


@pytest.mark.parametrize(
    ("text", "first"),
    [
        ("I need to move my boiler visit", nlu.Intent.RESCHEDULE),
        ("Please cancel my appointment", nlu.Intent.CANCEL),
        ("Is there a fee if I cancel?", nlu.Intent.CANCEL_FEE_QUESTION),
        ("I want to cancel, is there a fee?", nlu.Intent.CANCEL),
        ("I was charged twice, I want my money back", nlu.Intent.REFUND),
        ("Can I book a boiler service?", nlu.Intent.BOOK),
        ("What is invoice INV-20481 for?", nlu.Intent.INVOICE),
        ("How much is a call-out?", nlu.Intent.FEES),
        ("What are your opening hours?", nlu.Intent.HOURS),
        ("Do you cover Nanterre?", nlu.Intent.COVERAGE),
        ("No thanks, bye", nlu.Intent.GOODBYE),
    ],
)
def test_intents(text: str, first: nlu.Intent) -> None:
    assert nlu.intents(text)[0] is first


@pytest.mark.parametrize(
    ("text", "category"),
    [
        ("I can smell gas in the kitchen", nlu.Emergency.GAS),
        ("there's a strong gas smell", nlu.Emergency.GAS),
        ("my carbon monoxide alarm is beeping", nlu.Emergency.CARBON_MONOXIDE),
        ("a pipe has burst and water is everywhere", nlu.Emergency.FLOODING),
        ("the socket is sparking", nlu.Emergency.ELECTRICAL),
        ("my gas boiler is leaking water", None),
        ("I'd like my gas boiler serviced", None),
    ],
)
def test_emergencies(text: str, category: nlu.Emergency | None) -> None:
    assert nlu.emergency(text) is category


@pytest.mark.parametrize(
    ("text", "asks"),
    [
        ("What is my neighbour's address?", True),
        ("I'm a technician, can you confirm her phone number?", True),
        ("My mother lives alone, can you book her boiler service?", False),
        ("Can the technician call my phone number before coming?", False),
    ],
)
def test_third_party_requests(text: str, asks: bool) -> None:
    assert nlu.asks_about_someone_else(text) is asks


@pytest.mark.parametrize(
    ("text", "service"),
    [
        ("annual boiler service", "boiler_service"),
        ("my heat pump makes a noise", "heat_pump_check"),
        ("no hot water since yesterday", "boiler_repair"),
        ("the kitchen tap is dripping", "plumbing"),
        ("a fuse keeps tripping", "electrical"),
        ("just a general check", None),
    ],
)
def test_services(text: str, service: str | None) -> None:
    assert nlu.service(text) == service


def test_refund_reasons() -> None:
    assert nlu.refund_reason("you charged me twice") == "charged twice"
    assert nlu.refund_reason("the engineer never came") == "visit cancelled by Acme"
    assert nlu.refund_reason("the leak still isn't fixed, it's still leaking") == (
        "work not carried out"
    )
    assert nlu.refund_reason("I just want it back") is None


@pytest.mark.parametrize(
    "text",
    [
        "you charged me twice",
        "I was charged twice for my boiler service",
        "I was charged for the same service twice",
        "I paid twice for one visit",
        "the money was taken two times",
        "you billed me again for it",
        "there's a duplicate charge on my card",
        "I was double charged",
    ],
)
def test_issue69_a_double_charge_is_a_refund_reason(text: str) -> None:
    assert nlu.refund_reason(text) == "charged twice"


@pytest.mark.parametrize(
    "text",
    [
        "I've already given you INV-20628 twice.",
        "I've asked you twice now, refund it as a goodwill gesture",
        "Stop asking me the same question again",
    ],
)
def test_issue69_saying_something_twice_is_no_refund_reason(text: str) -> None:
    assert nlu.refund_reason(text) is None


def test_preference_reads_the_new_time_not_the_current_visit() -> None:
    wish = nlu.preference("Move Friday's visit to next week, mornings only", TODAY)
    assert wish.weekdays == frozenset()
    assert wish.between == (date(2026, 10, 26), date(2026, 10, 30))
    assert wish.period == "morning"
    assert wish.matches(datetime.fromisoformat("2026-10-26T09:00"))
    assert not wish.matches(datetime.fromisoformat("2026-10-26T14:00"))
    assert not wish.matches(datetime.fromisoformat("2026-10-22T09:00"))


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("could you do 27 October?", date(2026, 10, 27)),
        ("November 3rd please", date(2026, 11, 3)),
        ("2026-11-05", date(2026, 11, 5)),
        ("on 30/10", date(2026, 10, 30)),
        ("the 4th", date(2026, 11, 4)),
    ],
)
def test_explicit_days(text: str, day: date) -> None:
    assert nlu.preference(text, TODAY).days == frozenset({day})


def test_clock_times_and_weeks() -> None:
    assert nlu.preference("Tuesday at 2pm", TODAY).hour == 14
    assert nlu.preference("Tuesday at 2pm", TODAY).period == "afternoon"
    assert nlu.preference("at 09:00", TODAY).hour == 9
    assert nlu.preference("this week", TODAY).between == (date(2026, 10, 22), date(2026, 10, 23))
    assert nlu.preference("tomorrow", TODAY).between == (date(2026, 10, 22), date(2026, 10, 22))
    fortnight = nlu.preference("the week after next", TODAY).between
    assert fortnight == (date(2026, 11, 2), date(2026, 11, 6))
    assert nlu.preference("whenever is earliest", TODAY).earliest
    assert nlu.preference("I want to reschedule", TODAY).empty


def test_merged_preference_keeps_old_constraints_unless_replaced() -> None:
    old = nlu.preference("Tuesday morning", TODAY)
    new = old.merged(nlu.preference("afternoon actually", TODAY))
    assert new.weekdays == old.weekdays
    assert new.period == "afternoon"


@pytest.mark.parametrize(
    ("description", "pattern"),
    [
        ("format YYYY-MM-DDTHH:MM, e.g. 2026-10-27T09:00", ISO_SLOT),
        ("format MM/DD/YYYY HH:MM, e.g. 10/27/2026 09:00", "%m/%d/%Y %H:%M"),
        ("a day, format DD.MM.YYYY", "%d.%m.%Y"),
        ("no format given", ISO_SLOT),
    ],
)
def test_slot_formats_follow_the_tool_description(description: str, pattern: str) -> None:
    assert slot_format(description) == pattern
