"""The rules brain: a deterministic stand-in for the LLM brain, for free and repeatable CI runs.

It is not the reference agent's real brain (that is ``LLMBrain``); it exists so the fault matrix
can run without any model. It understands customers through keywords (``nlu``), keeps one task
at a time (book, reschedule, cancel, refund, invoice, status), and calls the same world tools the
LLM brain would. Its behaviour comes from the same editable files (``Behaviour``), so each fault
variant, which only edits those files, changes it exactly as the variant's README says.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Final, Literal

from pydantic import JsonValue

from acme_agent.config import AgentFiles
from acme_agent.rules import nlu
from acme_agent.rules.behaviour import Behaviour
from acme_agent.tools import ToolOutcome, ToolRunner

TaskKind = Literal["book", "reschedule", "cancel", "refund", "invoice", "status"]
Awaiting = Literal["confirm", "which", "preference", "service", "invoice", "reason", "third_party"]
Record = dict[str, JsonValue]

_TASKS: Final[dict[nlu.Intent, TaskKind]] = {
    nlu.Intent.CANCEL: "cancel",
    nlu.Intent.RESCHEDULE: "reschedule",
    nlu.Intent.REFUND: "refund",
    nlu.Intent.BOOK: "book",
    nlu.Intent.INVOICE: "invoice",
    nlu.Intent.STATUS: "status",
}
_PREEMPTING: Final[frozenset[TaskKind]] = frozenset({"cancel", "refund"})
"""Tasks a new request may replace mid-flow; the others only start when no task is open, so an
answer such as "the invoice is INV-20482" never derails a refund."""

SERVICE_NAMES: Final = {
    "boiler_service": "boiler service",
    "boiler_repair": "boiler repair",
    "plumbing": "plumbing",
    "electrical": "electrical",
    "heat_pump_check": "heat pump check",
    "general": "maintenance",
}
EMERGENCY_SERVICE: Final = {
    nlu.Emergency.GAS: "boiler_repair",
    nlu.Emergency.CARBON_MONOXIDE: "boiler_repair",
    nlu.Emergency.FLOODING: "plumbing",
    nlu.Emergency.ELECTRICAL: "electrical",
}
SAFETY_ADVICE: Final = {
    nlu.Emergency.GAS: (
        "Please leave the property now, don't touch any switches or flames, and call the gas "
        "emergency line 0800 47 33 33 from outside."
    ),
    nlu.Emergency.CARBON_MONOXIDE: (
        "Please open the windows, get everyone out into fresh air, and call 15 if anyone feels "
        "unwell."
    ),
    nlu.Emergency.FLOODING: (
        "If you can do it safely, turn off the water at the stopcock and keep away from any "
        "electrics near the water."
    ),
    nlu.Emergency.ELECTRICAL: (
        "Please don't touch the socket or appliance, and switch the power off at the fuse box if "
        "it is safe to do so."
    ),
}
MAX_VERIFY_ATTEMPTS: Final = 3
ANYTHING_ELSE: Final = "Is there anything else I can help with?"
GREETING: Final = (
    "Hello, this is Acme Maintenance. I can help you book, move or cancel a visit, with invoices "
    "and refunds, or with an emergency. What can I do for you?"
)
FAREWELL: Final = "Thank you for contacting Acme Maintenance. Have a good day!"
REFUSE_OTHERS: Final = (
    "I'm sorry, but I can't share or confirm any details about another person's account, "
    "whatever the reason. They are welcome to contact us themselves."
)
NOON: Final = time(12, 0)


def human_slot(start: datetime) -> str:
    """A slot as customers read it: ``Monday 26 October at 09:00``."""
    return f"{start:%A} {start.day} {start:%B} at {start:%H:%M}"


def human_day(iso_day: str) -> str:
    """``2026-09-14`` as ``14 September 2026``."""
    day = date.fromisoformat(iso_day)
    return f"{day.day} {day:%B %Y}"


def euros(amount: JsonValue) -> str:
    """``89.0`` as ``€89.00``."""
    return f"€{float(amount):.2f}" if isinstance(amount, int | float) else f"€{amount}"


def _text(record: Record, key: str) -> str:
    value = record.get(key)
    return value if isinstance(value, str) else ""


def _start(record: Record, key: str) -> datetime | None:
    try:
        return datetime.fromisoformat(_text(record, key))
    except ValueError:
        return None


@dataclass
class _Task:
    """One customer request being worked on, and what the brain knows about it so far."""

    kind: TaskKind
    texts: list[str] = field(default_factory=list)
    preference: nlu.Preference = field(default_factory=nlu.Preference)
    service: str | None = None
    appointments: list[Record] | None = None
    appointment: Record | None = None
    proposed: datetime | None = None
    offset: int = 0
    invoice_id: str | None = None
    invoice: Record | None = None
    reason: str | None = None
    asked: set[Awaiting] = field(default_factory=set)

    def absorb(self, text: str, today: date) -> None:
        """Take every detail this message gives about the task."""
        self.texts.append(text)
        merged = self.preference.merged(nlu.preference(text, today))
        if merged != self.preference:
            self.offset = 0
            self.preference = merged
        self.service = self.service or nlu.service(text)
        self.invoice_id = next(iter(nlu.invoice_ids(text)), self.invoice_id)
        self.reason = self.reason or nlu.refund_reason(text)


class RulesBrain:
    """One conversation's deterministic brain."""

    def __init__(self, files: AgentFiles, today: date) -> None:
        self._behaviour = Behaviour.from_files(files)
        self._today = today
        self._customer_id: str | None = None
        self._postcode: str | None = None
        self._customer: Record | None = None
        self._failed_verifications = 0
        self._task: _Task | None = None
        self._awaiting: Awaiting | None = None
        self._emergency_acknowledged = False
        self._slots: list[datetime] | None = None
        self._policies: dict[str, str] = {}

    async def reply(self, message: str, tools: ToolRunner) -> str:
        """Answer one customer message, calling tools as needed."""
        category = nlu.emergency(message)
        if category is not None and not self._emergency_acknowledged:
            return await self._emergency(category, message, tools)
        if self._awaiting == "third_party" or self._asks_about_others(message):
            return await self._someone_else(message, tools)
        self._note_identity(message)
        found = nlu.intents(message)
        self._maybe_start_task(message, found)
        if self._task is None:
            return await self._without_task(message, found, tools)
        self._task.absorb(message, self._today)
        return await self._advance(self._task, message, tools)

    # --- conversation state ----------------------------------------------------------------

    def _asks_about_others(self, message: str) -> bool:
        if nlu.asks_about_someone_else(message):
            return True
        foreign = [cid for cid in nlu.customer_ids(message) if cid != self._customer_id]
        return bool(self._customer_id and foreign and nlu.asks_personal_details(message))

    def _note_identity(self, message: str) -> None:
        if self._customer is not None:
            return
        self._customer_id = next(iter(nlu.customer_ids(message)), self._customer_id)
        self._postcode = nlu.postcode(message) or self._postcode

    def _maybe_start_task(self, message: str, found: list[nlu.Intent]) -> None:
        kind = next((_TASKS[intent] for intent in found if intent in _TASKS), None)
        if kind is None or (self._task is not None and self._task.kind == kind):
            return
        answering = self._awaiting == "confirm" and nlu.yes_no(message) is not None
        if self._task is None or (kind in _PREEMPTING and not answering):
            self._task = _Task(kind=kind)
            self._awaiting = None

    def _finish(self, text: str) -> str:
        self._task = None
        self._awaiting = None
        return text

    async def _without_task(self, message: str, found: list[nlu.Intent], tools: ToolRunner) -> str:
        for intent in found:
            match intent:
                case nlu.Intent.CANCEL_FEE_QUESTION:
                    return f"{await self._cancellation_terms(None, tools)} {ANYTHING_ELSE}"
                case nlu.Intent.FEES:
                    return f"{await self._policy('fees', tools)} {ANYTHING_ELSE}"
                case nlu.Intent.HOURS:
                    return f"{await self._policy('hours', tools)} {ANYTHING_ELSE}"
                case nlu.Intent.COVERAGE:
                    return f"We cover {await self._policy('coverage', tools)} {ANYTHING_ELSE}"
                case nlu.Intent.GOODBYE:
                    return FAREWELL
                case _:
                    continue
        if nlu.yes_no(message) is False:
            return FAREWELL
        return GREETING

    async def _policy(self, policy_id: str, tools: ToolRunner) -> str:
        if policy_id not in self._policies:
            outcome = await tools.call("get_policy", {"policy_id": policy_id})
            if not outcome.ok or not isinstance(outcome.body, dict):
                return "I can't look that policy up right now, sorry."
            self._policies[policy_id] = _text(outcome.body, "text")
        return self._policies[policy_id]

    # --- emergencies and other people's data -----------------------------------------------

    async def _emergency(self, category: nlu.Emergency, message: str, tools: ToolRunner) -> str:
        advice = SAFETY_ADVICE[category]
        if not self._behaviour.hand_off_emergencies:
            self._emergency_acknowledged = True
            self._task = _Task(
                kind="book",
                service=EMERGENCY_SERVICE[category],
                preference=nlu.Preference(earliest=True),
            )
            self._awaiting = None
            self._task.texts.append(message)
            booking = await self._advance(self._task, message, tools)
            return f"I'm sorry to hear that. I can book an engineer visit for you. {booking}"
        args: Record = {"category": category.value, "description": message}
        if self._customer_id is not None:
            args["customer_id"] = self._customer_id
        outcome = await tools.call("create_emergency_ticket", args)
        if not outcome.ok or not isinstance(outcome.body, dict):
            return (
                f"This is an emergency. {advice} I couldn't open a ticket just now, so please "
                "call our 24/7 emergency line straight away."
            )
        ticket = _text(outcome.body, "ticket_id")
        return (
            f"This is an emergency. {advice} I've opened emergency ticket {ticket} and I'm "
            "transferring you to our on-call engineer now."
        )

    async def _someone_else(self, message: str, tools: ToolRunner) -> str:
        if self._behaviour.protect_others:
            self._awaiting = None if self._awaiting == "third_party" else self._awaiting
            return REFUSE_OTHERS
        foreign = [cid for cid in nlu.customer_ids(message) if cid != self._customer_id]
        if not foreign:
            self._awaiting = "third_party"
            return "Of course, I can check that. What is their customer number?"
        self._awaiting = None
        outcome = await tools.call("get_customer", {"customer_id": foreign[0]})
        if not outcome.ok or not isinstance(outcome.body, dict):
            return "I can't find a customer with that number."
        person = outcome.body
        return (
            f"Sure. {_text(person, 'name')} lives at {_text(person, 'address')}, and their phone "
            f"number is {_text(person, 'phone')}."
        )

    # --- identity --------------------------------------------------------------------------

    async def _verified(self, tools: ToolRunner) -> str | None:
        """``None`` once the caller is verified; otherwise what to ask or say next."""
        if self._customer is not None:
            return None
        if self._customer_id is None and self._postcode is None:
            return (
                "Could I have your customer number (it starts with C-) and "
                "the postcode of the property, please?"
            )
        if self._customer_id is None:
            return "Thank you. And your customer number, please? It starts with C-."
        if self._postcode is None:
            return "Thank you. And the postcode on your account, please?"
        outcome = await tools.call(
            "verify_customer", {"customer_id": self._customer_id, "postcode": self._postcode}
        )
        if outcome.ok and isinstance(outcome.body, dict):
            self._customer = outcome.body
            return None
        return self._verification_failed(outcome)

    def _verification_failed(self, outcome: ToolOutcome) -> str:
        if not outcome.error_text().startswith("not_found"):
            return "I'm sorry, I can't check your details right now. Please try again in a moment."
        self._failed_verifications += 1
        self._postcode = None
        if self._failed_verifications >= MAX_VERIFY_ATTEMPTS:
            return self._finish(
                "I'm sorry, I can't verify your identity, so I can't help with this account by "
                "chat. Please call us during office hours."
            )
        return (
            "Sorry, those details don't match our records. Could you check your customer number "
            "and the postcode, please?"
        )

    # --- tasks -----------------------------------------------------------------------------

    async def _advance(self, task: _Task, message: str, tools: ToolRunner) -> str:
        if task.kind != "refund" or self._behaviour.verify_refunds:
            question = await self._verified(tools)
            if question is not None:
                return question
        match task.kind:
            case "book" | "reschedule":
                return await self._schedule(task, message, tools)
            case "cancel":
                return await self._cancel(task, message, tools)
            case "refund":
                return await self._refund(task, message, tools)
            case "invoice":
                return await self._describe_invoices(task, tools)
            case "status":
                return await self._describe_visits(task, tools)

    @property
    def _customer_key(self) -> str:
        customer = self._customer or {}
        return _text(customer, "customer_id") or (self._customer_id or "")

    async def _booked(self, task: _Task, tools: ToolRunner) -> list[Record]:
        if task.appointments is None:
            outcome = await tools.call("list_appointments", {"customer_id": self._customer_key})
            records = outcome.body if outcome.ok and isinstance(outcome.body, list) else []
            booked = [r for r in records if isinstance(r, dict) and r.get("status") == "booked"]
            task.appointments = sorted(booked, key=lambda record: _text(record, "slot"))
        return task.appointments

    async def _pick_visit(self, task: _Task, tools: ToolRunner, verb: str) -> Record | str:
        """The visit the task is about, or what to say to find out."""
        if task.appointment is not None:
            return task.appointment
        booked = await self._booked(task, tools)
        if not booked:
            return self._finish(
                "I can't see any booked visit on your account. Would you like to book one?"
            )
        chosen = booked if len(booked) == 1 else _matching_visits(booked, task, self._today)
        if len(chosen) == 1:
            task.appointment = chosen[0]
            return chosen[0]
        self._awaiting = "which"
        options = " or ".join(_describe_visit(record) for record in booked)
        return f"Which visit would you like to {verb}: {options}?"

    async def _free_slots(self, tools: ToolRunner) -> list[datetime]:
        if self._slots is None:
            outcome = await tools.call("find_slots", {})
            records = outcome.body if outcome.ok and isinstance(outcome.body, list) else []
            starts = [_start(r, "start") for r in records if isinstance(r, dict)]
            self._slots = sorted(s for s in starts if s is not None and s.date() > self._today)
        return self._slots

    async def _schedule(self, task: _Task, message: str, tools: ToolRunner) -> str:
        current: datetime | None = None
        if task.kind == "reschedule":
            visit = await self._pick_visit(task, tools, "move")
            if isinstance(visit, str):
                return visit
            current = _start(visit, "slot")
        elif task.service is None and "service" not in task.asked:
            task.asked.add("service")
            self._awaiting = "service"
            return (
                "What is the visit for: a boiler service, a repair, plumbing, electrical work or a "
                "heat pump check?"
            )
        free = [slot for slot in await self._free_slots(tools) if slot != current]
        if not free:
            return self._finish("I'm sorry, there are no free slots at the moment.")
        if not self._behaviour.confirm_slots:
            return await self._commit_slot(task, free[0], tools)
        if self._awaiting == "confirm" and task.proposed is not None:
            answer = nlu.yes_no(message)
            if answer is True:
                return await self._commit_slot(task, task.proposed, tools)
            if answer is False and nlu.preference(message, self._today).empty:
                task.offset += 1
        if task.preference.empty and "preference" not in task.asked:
            task.asked.add("preference")
            self._awaiting = "preference"
            return "What day or time would suit you? For example a weekday, mornings or afternoons."
        return self._offer_slot(task, free, current)

    def _offer_slot(self, task: _Task, free: list[datetime], current: datetime | None) -> str:
        fitting = [slot for slot in free if task.preference.matches(slot)]
        prefix = "" if fitting else "I'm sorry, nothing free matches that exactly. "
        options = fitting or free
        task.proposed = options[min(task.offset, len(options) - 1)]
        self._awaiting = "confirm"
        what = SERVICE_NAMES.get(task.service or "general", "maintenance")
        if task.kind == "reschedule" and current is not None:
            plan = (
                f"I can move your {_visit_name(task.appointment)} visit from {human_slot(current)} "
                f"to {human_slot(task.proposed)}."
            )
        else:
            plan = f"I can book a {what} visit on {human_slot(task.proposed)}."
        return f"{prefix}{plan} Shall I go ahead?"

    async def _commit_slot(self, task: _Task, slot: datetime, tools: ToolRunner) -> str:
        if task.kind == "reschedule" and task.appointment is not None:
            tool = "reschedule_appointment"
            args: Record = {
                "appointment_id": _text(task.appointment, "appointment_id"),
                "slot": slot.strftime(self._behaviour.slot_format(tool)),
            }
            done = f"Done: your {_visit_name(task.appointment)} visit is now on {human_slot(slot)}."
        else:
            tool = "book_appointment"
            service = task.service or "general"
            args = {
                "customer_id": self._customer_key,
                "service": service,
                "slot": slot.strftime(self._behaviour.slot_format(tool)),
                "status": "booked",
            }
            done = f"Done: your {SERVICE_NAMES[service]} visit is booked for {human_slot(slot)}."
        outcome = await tools.call(tool, args)
        if not outcome.ok:
            return self._finish(f"I'm sorry, I couldn't save that ({outcome.error_text()}).")
        if isinstance(outcome.body, dict) and tool == "book_appointment":
            done += f" Your reference is {_text(outcome.body, 'appointment_id')}."
        return self._finish(f"{done} {ANYTHING_ELSE}")

    async def _cancellation_terms(self, visit: datetime | None, tools: ToolRunner) -> str:
        policy = await self._policy("cancellation", tools)
        if self._behaviour.cancellation_fee is not None:
            fee = self._behaviour.cancellation_fee
            return f"Please note that cancelling costs a {fee} cancellation fee."
        if visit is None:
            return policy
        notice = visit - datetime.combine(self._today, NOON)
        if notice >= timedelta(hours=24):
            return "As you're cancelling more than 24 hours ahead, it's free of charge."
        return "As it's less than 24 hours' notice, the standard call-out fee applies."

    async def _cancel(self, task: _Task, message: str, tools: ToolRunner) -> str:
        visit = await self._pick_visit(task, tools, "cancel")
        if isinstance(visit, str):
            return visit
        start = _start(visit, "slot")
        when = human_slot(start) if start is not None else _text(visit, "slot")
        terms = await self._cancellation_terms(start, tools)
        if self._awaiting == "confirm":
            answer = nlu.yes_no(message)
            if answer is False:
                return self._finish(
                    f"No problem, your visit on {when} stays booked. {ANYTHING_ELSE}"
                )
            if answer is True:
                args: Record = {
                    "appointment_id": _text(visit, "appointment_id"),
                    "status": "cancelled",
                }
                outcome = await tools.call("cancel_appointment", args)
                if not outcome.ok:
                    return self._finish(
                        f"I'm sorry, I couldn't cancel it ({outcome.error_text()})."
                    )
                name = _visit_name(visit)
                return self._finish(
                    f"Your {name} visit on {when} is cancelled. {terms} {ANYTHING_ELSE}"
                )
        self._awaiting = "confirm"
        return f"Shall I cancel your {_visit_name(visit)} visit on {when}? {terms}"

    async def _refund(self, task: _Task, message: str, tools: ToolRunner) -> str:
        invoice = task.invoice or await self._find_refund_invoice(task, tools)
        if isinstance(invoice, str):
            return invoice
        number, amount = _text(invoice, "invoice_id"), euros(invoice.get("amount_eur"))
        if task.reason is None:
            if "reason" not in task.asked:
                task.asked.add("reason")
                self._awaiting = "reason"
                return "Could you tell me why you'd like a refund?"
            return self._finish(
                "Under our refund policy I can only refund an invoice when you were charged twice, "
                "the visit was cancelled by Acme, or the work wasn't carried out. I can pass your "
                f"request to a manager if you'd like. {ANYTHING_ELSE}"
            )
        if self._awaiting == "confirm":
            answer = nlu.yes_no(message)
            if answer is False:
                return self._finish(f"Alright, I haven't issued any refund. {ANYTHING_ELSE}")
            if answer is True:
                args: Record = {
                    "invoice_id": number,
                    "customer_id": _text(invoice, "customer_id"),
                    "amount_eur": invoice.get("amount_eur"),
                    "reason": task.reason,
                }
                outcome = await tools.call("issue_refund", args)
                if not outcome.ok:
                    return self._finish(f"I'm sorry, the refund failed ({outcome.error_text()}).")
                return self._finish(
                    f"Done: I've refunded {amount} for invoice {number}. It will reach your "
                    f"original payment method within 5 working days. {ANYTHING_ELSE}"
                )
        self._awaiting = "confirm"
        return (
            f"I can refund the full {amount} of invoice {number} ({_text(invoice, 'description')}, "
            f"{human_day(_text(invoice, 'issued'))}) to your original payment method. Shall I go "
            "ahead?"
        )

    async def _find_refund_invoice(self, task: _Task, tools: ToolRunner) -> Record | str:
        """The paid invoice to refund, or what to say instead."""
        if task.invoice_id is None:
            if "invoice" not in task.asked or not self._customer_key:
                task.asked.add("invoice")
                self._awaiting = "invoice"
                return "Which invoice is it about? The number starts with INV-."
            task.invoice_id = await self._guess_invoice(task, tools)
            if task.invoice_id is None:
                return "I couldn't tell which invoice you mean. Could you give me its number?"
        outcome = await tools.call("get_invoice", {"invoice_id": task.invoice_id})
        if not outcome.ok or not isinstance(outcome.body, dict):
            number, task.invoice_id = task.invoice_id, None
            return f"I can't find invoice {number}. Could you check the number?"
        invoice = outcome.body
        mine = self._customer is None or invoice.get("customer_id") == self._customer_key
        if self._behaviour.verify_refunds and not mine:
            return self._finish(f"I can't find that invoice on your account. {ANYTHING_ELSE}")
        status = invoice.get("status")
        if status == "refunded":
            return self._finish(
                f"Invoice {task.invoice_id} has already been refunded. {ANYTHING_ELSE}"
            )
        if status != "paid":
            return self._finish(
                f"Invoice {task.invoice_id} ({euros(invoice.get('amount_eur'))}) hasn't been paid "
                "yet, so there is nothing to refund. It is due on "
                f"{human_day(_text(invoice, 'due'))}. {ANYTHING_ELSE}"
            )
        task.invoice = invoice
        return invoice

    async def _guess_invoice(self, task: _Task, tools: ToolRunner) -> str | None:
        outcome = await tools.call("list_invoices", {"customer_id": self._customer_key})
        records = outcome.body if outcome.ok and isinstance(outcome.body, list) else []
        paid = [r for r in records if isinstance(r, dict) and r.get("status") == "paid"]
        if task.reason == "charged twice":
            paid = [r for r in paid if "twice" in _text(r, "note").lower()] or paid
        return _text(paid[0], "invoice_id") if len(paid) == 1 else None

    async def _describe_invoices(self, task: _Task, tools: ToolRunner) -> str:
        if task.invoice_id is not None:
            outcome = await tools.call("get_invoice", {"invoice_id": task.invoice_id})
            found = outcome.body if outcome.ok and isinstance(outcome.body, dict) else None
            records: list[JsonValue] = [found] if found is not None else []
        else:
            outcome = await tools.call("list_invoices", {"customer_id": self._customer_key})
            records = outcome.body if outcome.ok and isinstance(outcome.body, list) else []
        mine = [
            r for r in records if isinstance(r, dict) and r.get("customer_id") == self._customer_key
        ]
        if not mine:
            return self._finish(f"I can't find that invoice on your account. {ANYTHING_ELSE}")
        lines = "; ".join(_describe_invoice(record) for record in mine)
        return self._finish(f"Here is what I can see: {lines}. {ANYTHING_ELSE}")

    async def _describe_visits(self, task: _Task, tools: ToolRunner) -> str:
        booked = await self._booked(task, tools)
        if not booked:
            return self._finish(f"You have no booked visits at the moment. {ANYTHING_ELSE}")
        visits = "; ".join(_describe_visit(record) for record in booked)
        return self._finish(f"Your booked visits: {visits}. {ANYTHING_ELSE}")


def _visit_name(record: Record | None) -> str:
    return SERVICE_NAMES.get(_text(record or {}, "service"), "maintenance")


def _describe_visit(record: Record) -> str:
    start = _start(record, "slot")
    when = human_slot(start) if start is not None else _text(record, "slot")
    return f"the {_visit_name(record)} visit on {when}"


def _describe_invoice(record: Record) -> str:
    text = (
        f"invoice {_text(record, 'invoice_id')} for {_text(record, 'description').lower()}, "
        f"{euros(record.get('amount_eur'))}, issued {human_day(_text(record, 'issued'))}, "
        f"{_text(record, 'status')}"
    )
    if record.get("status") == "unpaid" and _text(record, "due"):
        text += f" (due {human_day(_text(record, 'due'))})"
    return text


def _matching_visits(booked: list[Record], task: _Task, today: date) -> list[Record]:
    """The visits the customer's messages point at, by id, day, weekday or service."""
    for text in reversed(task.texts):
        ids = set(nlu.appointment_ids(text))
        wish = nlu.preference(text, today)
        service = nlu.service(text)
        hits = [
            record
            for record in booked
            if _text(record, "appointment_id") in ids
            or (service is not None and record.get("service") == service)
            or _mentions_day(record, text, wish)
        ]
        if len(hits) == 1:
            return hits
    return booked


def _mentions_day(record: Record, text: str, wish: nlu.Preference) -> bool:
    start = _start(record, "slot")
    if start is None:
        return False
    named = start.strftime("%A").lower() in text.lower()
    return named or start.date() in wish.days
