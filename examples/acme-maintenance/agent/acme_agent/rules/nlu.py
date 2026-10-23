"""Keyword and regex understanding of customer messages for the rules brain.

Deliberately simple: identifiers, yes/no, intents, emergencies, services and slot preferences.
Every function is pure, so the rules brain is deterministic for a given conversation.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Final

_CUSTOMER_ID = re.compile(r"\bC\s?-?\s?(\d{4})\b", re.IGNORECASE)
_INVOICE_ID = re.compile(r"\bINV\s?-?\s?(\d{5})\b", re.IGNORECASE)
_APPOINTMENT_ID = re.compile(r"\bA-(\d{4})\b", re.IGNORECASE)
_POSTCODE = re.compile(r"(?<![\w-])(\d{5})(?![\w-])")


def customer_ids(text: str) -> list[str]:
    """Customer numbers in ``text``, normalized to ``C-1042``."""
    return [f"C-{digits}" for digits in _CUSTOMER_ID.findall(text)]


def invoice_ids(text: str) -> list[str]:
    """Invoice numbers in ``text``, normalized to ``INV-20481``."""
    return [f"INV-{digits}" for digits in _INVOICE_ID.findall(text)]


def appointment_ids(text: str) -> list[str]:
    """Visit ids in ``text``, normalized to ``A-3001``."""
    return [f"A-{digits}" for digits in _APPOINTMENT_ID.findall(text)]


def postcode(text: str) -> str | None:
    """A five-digit postcode, ignoring the digits of invoice numbers."""
    match = _POSTCODE.search(_INVOICE_ID.sub(" ", text))
    return match.group(1) if match else None


_NO = re.compile(
    r"^\W*(no|nope|nah|not really)\b(?!\s+(problem|worries))|\b(doesn'?t work|does not work|"
    r"can'?t (make|do)|cannot (make|do)|not that one|another (one|slot|time|day)|"
    r"different (one|slot|time|day)|rather not|don'?t want)\b",
    re.IGNORECASE,
)
_YES = re.compile(
    r"\b(yes|yeah|yep|yup|sure|ok|okay|alright|all right|perfect|great|fine|correct|absolutely|"
    r"definitely|please do|go ahead|do it|book it|confirm(ed)?|sounds good|that works|"
    r"works for me|lovely)\b",
    re.IGNORECASE,
)


def yes_no(text: str) -> bool | None:
    """``True`` for agreement, ``False`` for refusal, ``None`` when the message is neither."""
    if _NO.search(text):
        return False
    return True if _YES.search(text) else None


class Intent(StrEnum):
    """What the customer wants, in priority order of detection."""

    CANCEL_FEE_QUESTION = "cancel_fee_question"
    CANCEL = "cancel"
    RESCHEDULE = "reschedule"
    REFUND = "refund"
    BOOK = "book"
    INVOICE = "invoice"
    STATUS = "status"
    FEES = "fees"
    HOURS = "hours"
    COVERAGE = "coverage"
    GOODBYE = "goodbye"


_INTENTS: Final[tuple[tuple[Intent, re.Pattern[str]], ...]] = tuple(
    (intent, re.compile(pattern, re.IGNORECASE))
    for intent, pattern in (
        (Intent.CANCEL, r"\bcancel"),
        (
            Intent.RESCHEDULE,
            r"\b(re-?schedul\w*|move|moving|postpone|push (it )?back|bring (it )?forward|"
            r"rearrange|change (the |my )?(date|time|day|visit|appointment|slot)|"
            r"(different|another|other|new) (day|date|time|slot))\b",
        ),
        (
            Intent.REFUND,
            r"\b(refund\w*|money back|reimburs\w*|charged (me )?twice|double[- ]charged|"
            r"overcharged|paid twice)\b",
        ),
        (
            Intent.BOOK,
            r"\b(book\w*|schedule|new (visit|appointment)|arrange (a|an) (visit|appointment)|"
            r"send (an? |someone)|come (out|round|over)|annual service|service (my|the)|"
            r"get (my|the) \w+ serviced|need (a|an) (engineer|technician|plumber|electrician|"
            r"visit|appointment))",
        ),
        (Intent.INVOICE, r"\b(invoice|bill|billed|billing|charged?|payment|INV-?\d)"),
        (
            Intent.STATUS,
            r"\b(when is|when's|what time is|do i have|upcoming|next visit|"
            r"my (visit|appointment)s?\b.{0,20}\?)",
        ),
        (Intent.FEES, r"\b(how much|price|prices|cost|costs|fee|fees|rates?|charges?)\b"),
        (Intent.HOURS, r"\b(opening hours|office hours|are you open|open on|when are you open)\b"),
        (
            Intent.COVERAGE,
            r"\b(do you cover|service area|areas? (do )?you (cover|serve)|come to)\b",
        ),
        (
            Intent.GOODBYE,
            r"\b(bye|goodbye|that'?s all|that is all|nothing else|no,? thanks?|"
            r"have a (good|nice|great))\b",
        ),
    )
)

_WANTS_ACTION = re.compile(
    r"\b(want|need|like|would|please|can you|could you|have to|go ahead)\b.{0,25}\bcancel",
    re.IGNORECASE,
)


def intents(text: str) -> list[Intent]:
    """Every intent the message expresses, most specific first.

    A question about cancellation charges ("is there a fee if I cancel?") is a policy question,
    not a request to cancel, unless the customer also asks for the cancellation.
    """
    found = [intent for intent, pattern in _INTENTS if pattern.search(text)]
    if Intent.CANCEL in found and Intent.FEES in found and not _WANTS_ACTION.search(text):
        found.remove(Intent.CANCEL)
        found.insert(0, Intent.CANCEL_FEE_QUESTION)
    return found


class Emergency(StrEnum):
    """Emergency categories, as ``create_emergency_ticket`` names them."""

    GAS = "gas"
    CARBON_MONOXIDE = "carbon_monoxide"
    FLOODING = "flooding"
    ELECTRICAL = "electrical"


_EMERGENCIES: Final[tuple[tuple[Emergency, re.Pattern[str]], ...]] = tuple(
    (category, re.compile(pattern, re.IGNORECASE))
    for category, pattern in (
        (
            Emergency.GAS,
            r"\bgas\b.{0,15}\b(smell|odou?r|whiff|hiss)|"
            r"\b(smell|smells|smelling|odou?r|whiff|stench|reek)\w*\b.{0,25}\bgas\b|"
            r"gas (is )?leak|leaking gas",
        ),
        (
            Emergency.CARBON_MONOXIDE,
            r"carbon monoxide|\bCO (alarm|detector)|monoxide (alarm|detector)",
        ),
        (
            Emergency.FLOODING,
            r"\bflood|burst (pipe|water)|pipe (has )?burst|water (is )?(pouring|gushing|"
            r"everywhere|coming through|leaking through the ceiling)",
        ),
        (
            Emergency.ELECTRICAL,
            r"\bspark|burning smell|smell of burning|smells? (like|of) burning|"
            r"smok(e|ing) (from|coming)|electric(al)? shock|socket .{0,20}(melt|burn|smok)",
        ),
    )
)


def emergency(text: str) -> Emergency | None:
    """The emergency the customer reports, if any."""
    for category, pattern in _EMERGENCIES:
        if pattern.search(text):
            return category
    return None


_RELATION = re.compile(
    r"\b(neighbou?rs?|relatives?|mother|mum|mom|father|dad|parents?|brother|sister|wife|"
    r"husband|partner|son|daughter|aunt|uncle|grand\w+|cousin|friend|landlord|landlady|tenant|"
    r"flatmate|roommate|colleague|technician|previous owner|former owner|someone else|"
    r"another customer|other customer|on behalf of|his|her)\b",
    re.IGNORECASE,
)
_PERSONAL = re.compile(
    r"\b(address|phone|mobile|email|e-mail|contact details|details|where (he|she|they) lives?|"
    r"number for)\b",
    re.IGNORECASE,
)


_OWN_DETAILS = re.compile(r"\bmy (phone|address|e-?mail|mobile|number|details|contact)", re.I)
_THEIR_DETAILS = re.compile(
    r"\b(his|her|their)\b|'s (address|phone|e-?mail|mobile|number|details)", re.IGNORECASE
)


def asks_about_someone_else(text: str) -> bool:
    """Whether the message asks for a third party's personal details (not the caller's own)."""
    if not (_RELATION.search(text) and _PERSONAL.search(text)):
        return False
    return not _OWN_DETAILS.search(text) or bool(_THEIR_DETAILS.search(text))


def asks_personal_details(text: str) -> bool:
    """Whether the message asks for personal details at all (address, phone, email)."""
    return bool(_PERSONAL.search(text))


_SERVICES: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (service, re.compile(pattern, re.IGNORECASE))
    for service, pattern in (
        ("boiler_service", r"boiler service|annual service|service (my|the) boiler|serviced"),
        ("heat_pump_check", r"heat pump"),
        ("boiler_repair", r"boiler|no heating|no hot water|radiator|gas"),
        ("plumbing", r"plumb|leak|tap|toilet|sink|drain|pipe|shower|water"),
        ("electrical", r"electric|socket|fuse|wiring|lights?\b|spark|burning"),
    )
)


def service(text: str) -> str | None:
    """The kind of visit the customer needs, as ``book_appointment`` names it."""
    for name, pattern in _SERVICES:
        if pattern.search(text):
            return name
    return None


_REFUND_REASONS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (reason, re.compile(pattern, re.IGNORECASE))
    for reason, pattern in (
        # A repeat must be of a charge: "I've given you INV-20628 twice" is no reason (#69).
        (
            "charged twice",
            r"\b(charged|billed|paid|debited|taken|took|charge|payment)\b(\W+\w+){0,4}?\W+"
            r"(twice|two times|again)\b|\bdouble[- ]?(charged|charge|charging|billed|billing|"
            r"payment)\b|\bduplicate\b",
        ),
        (
            "visit cancelled by Acme",
            r"you cancelled|cancelled by (you|acme)|never (came|showed)|"
            r"no[- ]show|didn'?t (come|show)|did not (come|show)|visit was cancelled",
        ),
        (
            "work not carried out",
            r"not (done|carried out|fixed)|wasn'?t (done|fixed)|"
            r"didn'?t (do|fix)|did not (do|fix)|never (did|fixed)|still (broken|leaking)",
        ),
    )
)


def refund_reason(text: str) -> str | None:
    """The refund reason the policy accepts that the customer gives, if any."""
    for reason, pattern in _REFUND_REASONS:
        if pattern.search(text):
            return reason
    return None


# --- slot preferences ----------------------------------------------------------------------

_WEEKDAYS: Final = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_MONTHS: Final = (
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
)  # fmt: skip
_MONTH = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DAY_MONTH = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+{_MONTH}", re.IGNORECASE)
_MONTH_DAY = re.compile(rf"\b{_MONTH}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.IGNORECASE)
_ISO_DAY = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_SLASH_DAY = re.compile(r"\b(\d{1,2})/(\d{1,2})\b(?!/)")
_ORDINAL_DAY = re.compile(r"\bthe (\d{1,2})(?:st|nd|rd|th)\b", re.IGNORECASE)
_CLOCK = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\b(\d{1,2}):(\d{2})\b", re.IGNORECASE)
_MORNING = re.compile(r"\bmornings?\b|before (noon|lunch|midday)|\bearly\b", re.IGNORECASE)
_AFTERNOON = re.compile(r"\bafternoons?\b|after (noon|lunch|midday)", re.IGNORECASE)
_EARLIEST = re.compile(
    r"\b(earliest|asap|as soon as (possible|you can)|soonest|first (available|free)|any ?time|"
    r"whenever|don'?t mind|no preference|any day|urgent)\b",
    re.IGNORECASE,
)
_TARGET_MARKER = re.compile(r"\b(to|for|until|till|into|on)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Preference:
    """Constraints on a visit slot; an empty preference matches every slot."""

    days: frozenset[date] = frozenset()
    weekdays: frozenset[int] = frozenset()
    """Monday is 0, as ``date.weekday()``."""
    between: tuple[date, date] | None = None
    period: str | None = None
    """``morning`` or ``afternoon``."""
    hour: int | None = None
    earliest: bool = False
    """The customer takes the first free slot."""

    @property
    def empty(self) -> bool:
        """Whether nothing constrains the slot (and the customer did not say "any time")."""
        return self == Preference()

    def matches(self, start: datetime) -> bool:
        """Whether a slot starting at ``start`` satisfies every constraint."""
        day = start.date()
        period = "morning" if start.hour < 12 else "afternoon"
        return (
            (not self.days or day in self.days)
            and (not self.weekdays or day.weekday() in self.weekdays)
            and (self.between is None or self.between[0] <= day <= self.between[1])
            and (self.period is None or self.period == period)
            and (self.hour is None or self.hour == start.hour)
        )

    def merged(self, newer: "Preference") -> "Preference":
        """``newer`` refines this preference: each constraint it sets replaces the old one."""
        return Preference(
            days=newer.days or self.days,
            weekdays=newer.weekdays or self.weekdays,
            between=newer.between or self.between,
            period=newer.period or self.period,
            hour=newer.hour if newer.hour is not None else self.hour,
            earliest=newer.earliest or self.earliest,
        )


def preference(text: str, today: date) -> Preference:
    """The slot preference in ``text``, read from the part naming the new time when there is one.

    "Move Friday's visit to next week, mornings" describes the new slot after "to", so the
    current visit's "Friday" is not mistaken for a wish.
    """
    markers = list(_TARGET_MARKER.finditer(text))
    for marker in reversed(markers):
        tail = _parse_preference(text[marker.end() :], today)
        if not tail.empty:
            return tail
    return _parse_preference(text, today)


def _parse_preference(text: str, today: date) -> Preference:
    lowered = text.lower()
    weekdays = frozenset(
        index for index, name in enumerate(_WEEKDAYS) if re.search(rf"\b{name}s?\b", lowered)
    )
    hour = _hour(text)
    period = "morning" if _MORNING.search(text) else None
    period = "afternoon" if _AFTERNOON.search(text) else period
    if hour is not None and period is None:
        period = "morning" if hour < 12 else "afternoon"
    return Preference(
        days=_days(text, today),
        weekdays=weekdays,
        between=_week(lowered, today),
        period=period,
        hour=hour,
        earliest=bool(_EARLIEST.search(text)),
    )


def _week(lowered: str, today: date) -> tuple[date, date] | None:
    monday = today - timedelta(days=today.weekday())
    if re.search(r"week after next|in two weeks|in 2 weeks|fortnight", lowered):
        start = monday + timedelta(days=14)
    elif "next week" in lowered:
        start = monday + timedelta(days=7)
    elif "this week" in lowered:
        return today + timedelta(days=1), monday + timedelta(days=4)
    elif "tomorrow" in lowered:
        tomorrow = today + timedelta(days=1)
        return tomorrow, tomorrow
    else:
        return None
    return start, start + timedelta(days=4)


def _hour(text: str) -> int | None:
    match = _CLOCK.search(text)
    if match is None:
        return None
    if match.group(1) is not None:
        hour = int(match.group(1)) % 12
        return hour + 12 if match.group(3).lower() == "pm" else hour
    return int(match.group(4))


def _days(text: str, today: date) -> frozenset[date]:
    found: set[date] = set()
    for match in _DAY_MONTH.finditer(text):
        found.update(_on(today, _month(match.group(2)), int(match.group(1))))
    for match in _MONTH_DAY.finditer(text):
        found.update(_on(today, _month(match.group(1)), int(match.group(2))))
    for match in _ISO_DAY.finditer(text):
        found.update(_safe_date(int(match.group(1)), int(match.group(2)), int(match.group(3))))
    for match in _SLASH_DAY.finditer(text):
        found.update(_on(today, int(match.group(2)), int(match.group(1))))
    if not found:
        for match in _ORDINAL_DAY.finditer(text):
            day = int(match.group(1))
            # "the 27th" means the next 27th: this month if still ahead, else next month.
            month = today.month if day > today.day else today.month % 12 + 1
            found.update(_on(today, month, day))
    return frozenset(found)


def _month(name: str) -> int:
    return _MONTHS.index(name[:3].lower()) + 1


def _on(today: date, month: int, day: int) -> list[date]:
    """The next date with this month and day, on or after ``today``."""
    year = today.year if (month, day) >= (today.month, today.day) else today.year + 1
    return _safe_date(year, month, day)


def _safe_date(year: int, month: int, day: int) -> list[date]:
    try:
        return [date(year, month, day)]
    except ValueError:
        return []
