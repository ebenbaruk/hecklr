"""A reproducible synthetic production log with planted failure modes, to test the miner.

``generate_log_set(n, seed)`` writes conversations of a fictional maintenance company: mostly
successful ones (some deliberately close to a failure mode, e.g. two addresses handled well), five
planted failure modes, and a few unrelated one-off failures (off-topic requests the agent turns
down, which are not a planted mode). The ground truth (``planted``) is returned beside the
conversations, never inside them, so the miner cannot peek. Every conversation carries PII
(names, e-mails, phones, addresses), so scrubbing is exercised too.
"""

import random
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from pydantic import JsonValue

from hecklr_schemas import Conversation, ConversationToolCall, ConversationTurn, FailureCluster
from hecklr_schemas.events import Party

PLANTED_MODES: Final = {
    "two_addresses": "fails when the customer gives two addresses",
    "gas_no_handoff": "fails to hand off a gas emergency to a human",
    "invoice_loop": "keeps asking for an invoice number it was already given",
    "date_format": "books the wrong date when the customer writes day/month",
    "invented_fee": "invents a cancellation fee",
}
"""Planted failure mode -> the label a person would give its cluster."""
ONE_OFF: Final = "one_off"

_FIRST = (
    "Alice",
    "Bruno",
    "Chloe",
    "David",
    "Emma",
    "Farid",
    "Grace",
    "Hugo",
    "Ines",
    "Jack",
    "Karim",
    "Lea",
    "Marc",
    "Nina",
    "Oscar",
    "Paula",
    "Quentin",
    "Rosa",
    "Sami",
    "Tara",
)
_LAST = (
    "Martin",
    "Bernard",
    "Dubois",
    "Smith",
    "Jones",
    "Garcia",
    "Muller",
    "Rossi",
    "Silva",
    "Kowalski",
    "Nguyen",
    "Cohen",
    "Haddad",
    "Fischer",
    "Moreau",
    "Taylor",
)
_STREETS = (
    "Baker",
    "High",
    "Station",
    "Church",
    "Mill",
    "Park",
    "Victoria",
    "Green",
    "Oak",
    "Maple",
    "King",
    "Queen",
    "Elm",
    "Cedar",
    "Willow",
    "Bridge",
)
_STREET_TYPES = ("Street", "Road", "Avenue", "Lane", "Drive", "Close")
_APPLIANCES = ("boiler", "heat pump", "water heater", "furnace", "radiator system")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
_GREETINGS = ("Hi", "Hello", "Good morning", "Hey", "Hello there", "Good afternoon")
_THANKS = (
    "Thanks, perfect.",
    "Great, thank you!",
    "Thank you, that's all.",
    "Cheers!",
    "Perfect, thanks a lot.",
    "OK thanks, bye.",
)


@dataclass(frozen=True, slots=True)
class SyntheticLogSet:
    """Conversations and, separately, the failure mode planted in each (``None``: success)."""

    conversations: list[Conversation]
    planted: dict[str, str | None]

    def write_jsonl(self, path: Path) -> None:
        """Write the conversations as native JSONL (the ground truth is not written)."""
        path.write_text(
            "".join(c.model_dump_json() + "\n" for c in self.conversations), encoding="utf-8"
        )


class _Customer:
    """Random but consistent personal details for one conversation."""

    def __init__(self, rng: random.Random) -> None:
        self.first, self.last = rng.choice(_FIRST), rng.choice(_LAST)
        self.email = f"{self.first.lower()}.{self.last.lower()}{rng.randint(1, 99)}@example.com"
        self.phone = f"+44 7{rng.randint(100, 999)} {rng.randint(100000, 999999)}"
        self.addresses = [self._address(rng) for _ in range(2)]
        self.invoice = f"INV-{rng.randint(10000, 99999)}"
        self.appliance = rng.choice(_APPLIANCES)
        self.day = rng.choice(_DAYS)
        self.other_day = rng.choice([d for d in _DAYS if d != self.day])

    @staticmethod
    def _address(rng: random.Random) -> str:
        return f"{rng.randint(1, 250)} {rng.choice(_STREETS)} {rng.choice(_STREET_TYPES)}"

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}"


Script = list[tuple[Party, str, list[ConversationToolCall]]]


def _say(
    role: Party, text: str, *calls: ConversationToolCall
) -> tuple[Party, str, list[ConversationToolCall]]:
    return role, text, list(calls)


def _call(tool: str, result: JsonValue = None, **args: JsonValue) -> ConversationToolCall:
    return ConversationToolCall(
        tool=tool, args=args, result=result if result is not None else {"ok": True}
    )


def _two_addresses(c: _Customer, rng: random.Random) -> Script:
    billing, site = c.addresses
    opener = rng.choice(
        [
            f"my billing address is {billing} but the {c.appliance} is at my rental at {site}.",
            f"the invoice goes to {billing}, but please send the engineer to {site}.",
            f"I have two addresses: bills go to {billing} and the visit should be at {site}.",
        ]
    )
    return [
        _say(
            "user", f"{rng.choice(_GREETINGS)}, this is {c.name}. I need a repair visit; {opener}"
        ),
        _say(
            "agent",
            f"Thanks {c.first}! I've booked the technician at {billing} on {c.day}.",
            _call("book_visit", address=billing, day=c.day),
        ),
        _say(
            "user",
            rng.choice(
                [
                    f"No, that's my billing address. The technician must go to {site}!",
                    f"Wrong address. {billing} is only for invoices, the visit is at {site}.",
                    f"That's the wrong address, I said the repair is at {site}, not {billing}.",
                ]
            ),
        ),
        _say("agent", f"I'm sorry, I can only book visits at the address on file, {billing}."),
        _say(
            "user",
            rng.choice(
                [
                    "This is ridiculous. Let me speak to a human.",
                    "Useless. I want to talk to a real person.",
                    "Unacceptable. Forget it, I'll call back.",
                ]
            ),
        ),
    ]


def _gas_no_handoff(c: _Customer, rng: random.Random) -> Script:
    return [
        _say(
            "user",
            rng.choice(
                [
                    f"I can smell gas near my {c.appliance} in the kitchen.",
                    f"There's a strong gas smell coming from the {c.appliance}, what should I do?",
                    "I think there is a gas leak in my house, it smells of gas everywhere.",
                ]
            ),
        ),
        _say(
            "agent",
            f"I can book a routine maintenance visit. The first free slot is {c.day}.",
            _call("find_slots", result={"slots": [c.day]}, kind="routine"),
        ),
        _say(
            "user",
            rng.choice(
                [
                    "A routine visit?? It smells of gas right now, isn't this an emergency?",
                    "Routine? There is gas in my kitchen, I need someone now!",
                    "No maintenance, the gas smell is getting stronger. Isn't that urgent?",
                ]
            ),
        ),
        _say("agent", f"Our next available routine slot is {c.day}. Shall I book it for you?"),
        _say(
            "user",
            rng.choice(
                [
                    "Unbelievable. Forget it, I'm calling the gas emergency line.",
                    "This is dangerous and useless. Never mind, I'll call the emergency number.",
                    "Seriously? Forget it.",
                ]
            ),
        ),
    ]


def _invoice_loop(c: _Customer, rng: random.Random) -> Script:
    inv = c.invoice
    return [
        _say(
            "user",
            rng.choice(
                [
                    f"I was charged twice for invoice {inv}. Can you refund the duplicate payment?",
                    f"Hello, invoice {inv} was paid twice from my card, refund the double charge.",
                    f"You billed me two times for {inv}. Please refund one payment.",
                ]
            ),
        ),
        _say("agent", "I can help with that refund. Could you give me your invoice number?"),
        _say(
            "user",
            rng.choice(
                [
                    f"It's {inv}, as I said.",
                    f"{inv}, it's in my first message.",
                    f"The invoice number is {inv}.",
                ]
            ),
        ),
        _say(
            "agent",
            "Thanks. Could you give me your invoice number so I can look it up?",
            _call("find_invoice", result={"error": "not found"}, number=""),
        ),
        _say(
            "user",
            rng.choice(
                [
                    f"I already gave you the invoice number {inv} twice! This is a waste of time.",
                    f"Again? {inv}! I have told you the invoice number already, this is useless.",
                    f"Seriously, it's {inv}. I'm going in circles with you, forget it.",
                ]
            ),
        ),
    ]


def _date_format(c: _Customer, rng: random.Random) -> Script:
    day, month = rng.randint(1, 12), rng.randint(1, 12)
    while day == month:
        month = rng.randint(1, 12)
    written = f"{day:02d}/{month:02d}"
    booked = f"{_MONTHS[day - 1]} {month}"
    meant = f"{day} {_MONTHS[month - 1]}"
    return [
        _say(
            "user",
            rng.choice(
                [
                    f"Please book the annual {c.appliance} service for {written}.",
                    f"Can I have my yearly service on {written}? Mornings are best.",
                    f"I'd like the {c.appliance} checked on {written} please.",
                ]
            ),
        ),
        _say(
            "agent",
            f"Done! Your service is booked for {booked}.",
            _call("book_visit", date=f"2027-{day:02d}-{month:02d}"),
        ),
        _say(
            "user",
            rng.choice(
                [
                    f"No, I wrote {written}, that's {meant}, not {booked}!",
                    f"That's the wrong date. {written} means {meant}.",
                    f"Wrong month: I meant {meant}, day first.",
                ]
            ),
        ),
        _say("agent", f"Your booking for {booked} is confirmed."),
        _say(
            "user",
            rng.choice(
                [
                    "That's wrong again. Terrible service.",
                    "You keep getting the date wrong. Awful.",
                    "Unbelievable, the date is still wrong. Very disappointing.",
                ]
            ),
        ),
    ]


def _invented_fee(c: _Customer, rng: random.Random) -> Script:
    fee = rng.choice((25, 30, 40, 49, 59))
    return [
        _say(
            "user",
            rng.choice(
                [
                    f"I need to cancel my {c.appliance} appointment on {c.day}.",
                    f"Hi, please cancel the visit planned for {c.day}.",
                    f"Can you cancel my booking on {c.day}? I won't be home.",
                ]
            ),
        ),
        _say("agent", f"I can cancel it, but there is a cancellation fee of {fee} euros."),
        _say(
            "user",
            rng.choice(
                [
                    "What fee? Your website says cancellations are free.",
                    "A cancellation fee? Nobody told me about any fee.",
                    f"{fee} euros to cancel? That was never in my contract.",
                ]
            ),
        ),
        _say(
            "agent",
            f"The {fee} euro cancellation fee applies to all appointments.",
            _call("cancel_appointment", day=c.day, fee=fee),
        ),
        _say(
            "user",
            rng.choice(
                [
                    "That's outrageous, I want to talk to a manager.",
                    "This fee is a joke. Let me speak to a supervisor.",
                    "Unacceptable. I want to speak to a human about this fee.",
                ]
            ),
        ),
    ]


def _reschedule(c: _Customer, rng: random.Random) -> Script:
    return [
        _say(
            "user",
            f"{rng.choice(_GREETINGS)}, can I move my {c.appliance} visit from {c.day} "
            f"to {c.other_day}?",
        ),
        _say(
            "agent",
            f"Sure, I've moved your visit to {c.other_day} morning.",
            _call("reschedule_appointment", day=c.other_day),
        ),
        _say("user", rng.choice(_THANKS)),
    ]


def _invoice_copy(c: _Customer, rng: random.Random) -> Script:
    return [
        _say("user", f"{rng.choice(_GREETINGS)}, could you send me a copy of invoice {c.invoice}?"),
        _say(
            "agent",
            f"Of course, I've emailed {c.invoice} to {c.email}.",
            _call("send_invoice", number=c.invoice, email=c.email),
        ),
        _say("user", rng.choice(_THANKS)),
    ]


def _new_booking(c: _Customer, rng: random.Random) -> Script:
    return [
        _say(
            "user",
            f"I'd like a {c.appliance} service at {c.addresses[0]}. I'm {c.name}, phone {c.phone}.",
        ),
        _say(
            "agent",
            f"Booked for {c.day} at {c.addresses[0]}. A confirmation is on its way to {c.email}.",
            _call("book_visit", address=c.addresses[0], day=c.day),
        ),
        _say("user", rng.choice(_THANKS)),
    ]


def _two_addresses_handled(c: _Customer, rng: random.Random) -> Script:
    billing, site = c.addresses
    return [
        _say("user", f"Bills go to {billing} but the {c.appliance} is at {site}."),
        _say(
            "agent",
            f"Got it: the technician goes to {site} on {c.day} and the invoice to {billing}.",
            _call("book_visit", address=site, billing=billing),
        ),
        _say("user", rng.choice(_THANKS)),
    ]


def _opening_hours(c: _Customer, rng: random.Random) -> Script:
    return [
        _say("user", f"{rng.choice(_GREETINGS)}, what are your opening hours on {c.day}?"),
        _say("agent", "We're open from 8am to 6pm, Monday to Friday."),
        _say("user", rng.choice(_THANKS)),
    ]


def _update_phone(c: _Customer, rng: random.Random) -> Script:
    return [
        _say("user", f"Please update my phone number to {c.phone}. My email is {c.email}."),
        _say(
            "agent",
            "Done, your phone number is updated.",
            _call("update_customer", phone=c.phone, email=c.email),
        ),
        _say("user", rng.choice(_THANKS)),
    ]


_ONE_OFF_TOPICS: Final = (
    ("My parrot learned to imitate the thermostat beep and now it never stops.", "parrot"),
    ("Can your technician also tune my grand piano while he is here?", "piano"),
    ("The smart meter app shows my energy use in Klingon.", "klingon"),
    ("My neighbour's drone keeps landing on my outdoor heat exchanger.", "drone"),
    ("Is it safe to dry socks on the hot water cylinder in the loft?", "socks"),
    ("The installer left a sandwich inside the airing cupboard last year.", "sandwich"),
    ("Can I pay my invoice in cryptocurrency or gold coins?", "cryptocurrency"),
    ("My cat sleeps on the boiler, will the warranty still cover fur damage?", "warranty fur"),
    ("Do you sponsor the local football team? I'd like tickets.", "football"),
    ("The engineer's van blocked my driveway during my wedding.", "wedding"),
    ("Can you install solar panels on a houseboat?", "houseboat"),
    ("My grandmother wants the radiator painted pink to match the curtains.", "pink paint"),
)


_ONE_OFF_REPLIES: Final = (
    "Hmm, {topic} is outside what I know.",
    "Our team doesn't handle {topic}.",
    "I have no information about {topic}, sorry.",
    "That {topic} request isn't something we offer.",
)
_ONE_OFF_ENDINGS: Final = ("Never mind then.", "Forget it.", "Pointless. Bye", "I give up.")


def _one_off(c: _Customer, rng: random.Random) -> Script:
    # Each one-off is unlike the others (topic, reply and ending all vary), so a good miner
    # leaves them as noise instead of inventing a cluster.
    question, topic = rng.choice(_ONE_OFF_TOPICS)
    return [
        _say("user", question),
        _say("agent", rng.choice(_ONE_OFF_REPLIES).format(topic=topic)),
        _say("user", rng.choice(_ONE_OFF_ENDINGS)),
    ]


_FAILURES: Final[dict[str, Callable[[_Customer, random.Random], Script]]] = {
    "two_addresses": _two_addresses,
    "gas_no_handoff": _gas_no_handoff,
    "invoice_loop": _invoice_loop,
    "date_format": _date_format,
    "invented_fee": _invented_fee,
}
_SUCCESSES: Final = (
    _reschedule,
    _invoice_copy,
    _new_booking,
    _two_addresses_handled,
    _opening_hours,
    _update_phone,
)


def generate_log_set(
    n: int = 1000, *, seed: int = 0, failure_share: float = 0.4, one_off_share: float = 0.02
) -> SyntheticLogSet:
    """``n`` seeded conversations: planted failures, one-off failures, and successes.

    ``failure_share`` of them (in expectation) are planted failures split evenly over the five
    modes, ``one_off_share`` unrelated one-off failures, and the rest successes.
    """
    rng = random.Random(seed)  # noqa: S311 -- synthetic test data, not security
    origin = datetime(2026, 9, 1, tzinfo=UTC)
    conversations, planted = [], {}
    for index in range(n):
        draw = rng.random()
        customer = _Customer(rng)
        mode: str | None
        if draw < failure_share:
            mode = rng.choice(sorted(_FAILURES))
            script = _FAILURES[mode](customer, rng)
        elif draw < failure_share + one_off_share:
            mode, script = ONE_OFF, _one_off(customer, rng)
        else:
            mode, script = None, rng.choice(_SUCCESSES)(customer, rng)
        cid = f"prod-{index:05d}"
        conversations.append(
            _conversation(cid, script, origin + timedelta(minutes=rng.randint(0, 43_200)), rng)
        )
        planted[cid] = mode
    return SyntheticLogSet(conversations=conversations, planted=planted)


def _conversation(
    cid: str, script: Script, started_at: datetime, rng: random.Random
) -> Conversation:
    t_ms, turns = 0, []
    for role, text, calls in script:
        turns.append(ConversationTurn(role=role, text=text, t_ms=t_ms, tool_calls=calls))
        t_ms += rng.randint(1_500, 20_000)
    return Conversation(
        id=cid,
        source="jsonl",
        started_at=started_at,
        turns=turns,
        metadata={"channel_detail": "web-chat"},
    )


@dataclass(frozen=True, slots=True)
class Recovery:
    """How well a mining run recovered the planted failure modes."""

    detection_recall: float
    """Share of planted failures (one-offs included) flagged as failed."""
    false_positive_rate: float
    """Share of successful conversations flagged as failed."""
    purity: float
    """Share of clustered conversations whose cluster's majority mode is their own mode."""
    mode_recall: dict[str, float]
    """Per planted mode: share of its conversations in clusters whose majority is that mode."""
    clusters_of_mode: dict[str, list[str]]
    """Per planted mode: ids of the clusters whose majority is that mode."""

    @property
    def recovered(self) -> set[str]:
        """Planted modes that got at least one cluster of their own."""
        return {mode for mode, clusters in self.clusters_of_mode.items() if clusters}


def evaluate_recovery(
    planted: Mapping[str, str | None],
    failed_ids: Iterable[str],
    clusters: Sequence[FailureCluster],
) -> Recovery:
    """Score clusters against the ground truth of a synthetic log set."""
    failed = set(failed_ids)
    bad = {cid for cid, mode in planted.items() if mode is not None}
    good = set(planted) - bad
    clusters_of_mode: dict[str, list[str]] = {mode: [] for mode in PLANTED_MODES}
    agreeing, clustered = 0, 0
    recalled: Counter[str] = Counter()
    for cluster in clusters:
        modes = Counter(planted[cid] for cid in cluster.conversation_ids)
        majority, count = modes.most_common(1)[0]
        agreeing += count
        clustered += cluster.size
        if majority in clusters_of_mode:
            clusters_of_mode[majority].append(cluster.id)
            recalled[majority] += count
    totals = Counter(mode for mode in planted.values() if mode in PLANTED_MODES)
    return Recovery(
        detection_recall=len(bad & failed) / len(bad) if bad else 1.0,
        false_positive_rate=len(good & failed) / len(good) if good else 0.0,
        purity=agreeing / clustered if clustered else 0.0,
        mode_recall={
            mode: recalled[mode] / totals[mode] if totals[mode] else 0.0 for mode in PLANTED_MODES
        },
        clusters_of_mode=clusters_of_mode,
    )
