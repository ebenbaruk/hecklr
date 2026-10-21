"""The clean rules brain on the key flows: scheduling, cancelling, billing, refunds, emergencies."""

from collections.abc import Callable, Mapping

import pytest
from acme_agent import AcmeAgent, AgentSettings, ToolOutcome, load_agent_files
from acme_agent.rules.brain import FAREWELL, GREETING, REFUSE_OTHERS
from acme_agent.tools import LocalWorldTools
from acme_testing import Chat
from pydantic import JsonValue

from hecklr_schemas import World

ChatFactory = Callable[..., Chat]

pytestmark = pytest.mark.anyio


async def test_reschedule_confirms_the_slot_matching_the_preference(chat: ChatFactory) -> None:
    conversation = chat()
    first, offer, done = await conversation.script(
        "Hi, I need to move my boiler visit on Friday to next week, mornings only please.",
        "C-1042, postcode 92000",
        "Yes please",
    )
    assert "customer number" in first
    assert "Monday 26 October at 09:00" in offer
    assert offer.endswith("Shall I go ahead?")
    assert "now on Monday 26 October at 09:00" in done
    assert conversation.record("appointments", "A-3001")["slot"] == "2026-10-26T09:00"
    assert conversation.tools_called()[:2] == ["verify_customer", "list_appointments"]


async def test_reschedule_offers_the_next_slot_after_a_no(chat: ChatFactory) -> None:
    conversation = chat()
    replies = await conversation.script(
        "I'd like to reschedule my visit please", "C-1188 75005", "Thursdays", "no", "yes"
    )
    assert "What day or time would suit you?" in replies[1]
    assert "Thursday 22 October at 09:00" in replies[2]
    assert "Thursday 22 October at 14:00" in replies[3]
    assert conversation.record("appointments", "A-3003")["slot"] == "2026-10-22T14:00"


async def test_reschedule_asks_which_visit_when_there_are_several(chat: ChatFactory) -> None:
    conversation = chat()
    which, when, offer, _ = await conversation.script(
        "C-1042 92000, I need to change the time of a visit",
        "the plumbing one",
        "Tuesday morning",
        "yes",
    )
    assert "Which visit would you like to move" in which
    assert "What day or time" in when
    assert "plumbing visit from Monday 9 November at 14:00 to Tuesday 27 October" in offer
    assert conversation.record("appointments", "A-3012")["slot"] == "2026-10-27T09:00"
    assert conversation.record("appointments", "A-3001")["slot"] == "2026-10-23T09:00"


async def test_a_preference_nothing_matches_falls_back_to_the_earliest(chat: ChatFactory) -> None:
    conversation = chat()
    replies = await conversation.script("Move my visit to a Saturday please", "C-1306 92200")
    assert replies[1].startswith("I'm sorry, nothing free matches that exactly.")


async def test_booking_asks_the_service_then_the_time(chat: ChatFactory) -> None:
    conversation = chat()
    replies = await conversation.script(
        "I'd like to book a visit",
        "C-1107",
        "75011",
        "my kitchen tap is leaking",
        "Tuesday afternoon",
        "yes",
    )
    assert "postcode" in replies[1]
    assert "What is the visit for" in replies[2]
    assert "What day or time" in replies[3]
    assert "plumbing visit on Tuesday 27 October at 14:00" in replies[4]
    [booked] = conversation.created("appointments")
    assert booked == {
        "appointment_id": "appointments-15",
        "customer_id": "C-1107",
        "service": "plumbing",
        "slot": "2026-10-27T14:00",
        "status": "booked",
    }
    assert "Your reference is appointments-15" in replies[5]


async def test_cancel_confirms_and_states_the_real_policy(chat: ChatFactory) -> None:
    conversation = chat()
    _, question, done = await conversation.script(
        "I want to cancel my appointment", "C-1188 75005", "yes"
    )
    assert question.startswith("Shall I cancel your boiler service visit on Wednesday 28 October")
    assert "free of charge" in question
    assert "€" not in question + done
    assert conversation.record("appointments", "A-3003")["status"] == "cancelled"
    assert "get_policy" in conversation.tools_called()


async def test_cancel_declined_keeps_the_visit(chat: ChatFactory) -> None:
    conversation = chat()
    replies = await conversation.script("Cancel my visit please", "C-1215 92100", "no")
    assert "stays booked" in replies[2]
    assert conversation.record("appointments", "A-3004")["status"] == "booked"


async def test_refund_verifies_then_confirms(chat: ChatFactory) -> None:
    conversation = chat()
    replies = await conversation.script(
        "I was charged twice and want a refund",
        "C-1042 92000",
        "I don't know the invoice number",
        "yes",
    )
    assert "INV-20482" in replies[2]
    assert "Shall I go ahead?" in replies[2]
    assert conversation.tools_called()[0] == "verify_customer"
    [refund] = conversation.created("refunds")
    assert refund["invoice_id"] == "INV-20482"
    assert refund["amount_eur"] == 89.0
    assert refund["reason"] == "charged twice"


async def test_refund_without_an_accepted_reason_is_declined(chat: ChatFactory) -> None:
    conversation = chat()
    replies = await conversation.script(
        "Can I get a refund on INV-20628?", "C-1609 92300", "I just changed my mind"
    )
    assert "why" in replies[1]
    assert "Under our refund policy" in replies[2]
    assert conversation.created("refunds") == []


@pytest.mark.parametrize(
    ("invoice", "customer", "postcode", "expected"),
    [
        ("INV-20590", "C-1351", "75020", "hasn't been paid"),
        ("INV-20633", "C-1744", "92250", "already been refunded"),
        ("INV-20655", "C-1042", "92000", "can't find that invoice on your account"),
        ("INV-99999", "C-1042", "92000", "can't find invoice INV-99999"),
    ],
)
async def test_refund_refusals(
    chat: ChatFactory, invoice: str, customer: str, postcode: str, expected: str
) -> None:
    conversation = chat()
    replies = await conversation.script(
        f"Refund invoice {invoice} please, the engineer never came", f"{customer} {postcode}"
    )
    assert expected in replies[1]
    assert conversation.created("refunds") == []


async def test_three_failed_verifications_end_the_task(chat: ChatFactory) -> None:
    conversation = chat()
    replies = await conversation.script(
        "I want a refund for INV-20655, I was charged twice",
        "I'm C-2077, postcode 75009",
        "75008",
        "75010",
    )
    assert "don't match" in replies[1]
    assert "don't match" in replies[2]
    assert "can't verify your identity" in replies[3]
    assert conversation.created("refunds") == []
    assert conversation.tools_called().count("verify_customer") == 3


async def test_gas_smell_is_handed_off_at_once(chat: ChatFactory) -> None:
    conversation = chat()
    reply = await conversation.say("Help, I can smell gas in my kitchen!")
    [turn] = conversation.replies
    assert "leave the property" in reply
    assert "transferring you to our on-call engineer" in reply
    assert turn.handoff is not None
    assert turn.ended
    [ticket] = conversation.created("emergency_tickets")
    assert ticket["category"] == "gas"


async def test_an_emergency_mid_task_still_comes_first(chat: ChatFactory) -> None:
    conversation = chat()
    await conversation.script("I'd like to book a boiler service", "C-1420")
    await conversation.say("oh no, water is pouring through the ceiling, a pipe has burst!")
    assert conversation.replies[-1].handoff is not None
    [ticket] = conversation.created("emergency_tickets")
    assert ticket == {
        "ticket_id": "emergency_tickets-1",
        "category": "flooding",
        "description": "oh no, water is pouring through the ceiling, a pipe has burst!",
        "customer_id": "C-1420",
    }


async def test_other_customers_details_are_refused(chat: ChatFactory) -> None:
    conversation = chat()
    reply = await conversation.say("I'm calling for my neighbour C-2077, what's his address?")
    assert reply == REFUSE_OTHERS
    assert conversation.tools_called() == []


async def test_a_verified_caller_cannot_look_up_another_number(chat: ChatFactory) -> None:
    conversation = chat()
    await conversation.script("When is my next visit?", "C-1042 92000")
    reply = await conversation.say("And what is the phone number for C-2077?")
    assert reply == REFUSE_OTHERS


async def test_invoice_questions_are_answered_from_the_record(chat: ChatFactory) -> None:
    conversation = chat()
    replies = await conversation.script("What is invoice INV-20590 for?", "C-1351 75020")
    assert (
        "pump replacement, €312.40, issued 5 October 2026, unpaid (due 4 November 2026)"
        in (replies[1])
    )


async def test_all_invoices_are_listed_without_a_number(chat: ChatFactory) -> None:
    conversation = chat()
    replies = await conversation.script("Can you check my bills?", "C-1042 92000")
    assert replies[1].count("invoice INV-") == 2


async def test_visit_status(chat: ChatFactory) -> None:
    conversation = chat()
    replies = await conversation.script("When is my next visit?", "C-1802 75006")
    assert "plumbing visit on Monday 26 October at 14:00" in replies[1]


async def test_no_booked_visit_to_move(chat: ChatFactory) -> None:
    conversation = chat()
    await conversation.script("I need to cancel my appointment", "C-1215 92100", "yes")
    reply = await conversation.say("Actually I also want to move my visit")
    assert "can't see any booked visit" in reply


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("How much is a boiler service?", "Annual boiler service: 89 EUR"),
        ("Is there a fee if I cancel?", "free of charge with at least 24 hours' notice"),
        ("What are your opening hours?", "Monday to Friday 08:00-18:00"),
        ("Do you cover Levallois?", "Hauts-de-Seine"),
        ("No thanks, bye", FAREWELL),
        ("Hello", GREETING),
        ("no", FAREWELL),
    ],
)
async def test_information_questions(chat: ChatFactory, question: str, expected: str) -> None:
    assert expected in await chat().say(question)


class _BrokenBackend:
    reports_calls = True

    async def call(self, name: str, args: Mapping[str, JsonValue]) -> ToolOutcome:
        return ToolOutcome(ok=False, body={"error": {"code": "timeout", "message": "no answer"}})


async def test_tool_outages_are_explained_not_raised() -> None:
    agent = AcmeAgent(load_agent_files(), AgentSettings(brain="rules"))
    backend = _BrokenBackend()
    replies = [
        await agent.respond("s", text, lambda: backend)
        for text in ("I smell gas!", "How much is a call-out?", "Move my visit", "C-1042 92000")
    ]
    assert "couldn't open a ticket" in replies[0].text
    assert replies[0].handoff is None
    assert "can't look that policy up" in replies[1].text
    assert "can't check your details right now" in replies[3].text


async def test_sessions_keep_their_own_state_and_world(world: World) -> None:
    agent = AcmeAgent(load_agent_files(), AgentSettings(brain="rules"))
    worlds = {"a": LocalWorldTools(world), "b": LocalWorldTools(world)}

    async def say(session: str, text: str) -> str:
        return (await agent.respond(session, text, lambda: worlds[session])).text

    await say("a", "Please cancel my visit")
    await say("a", "C-1188 75005")
    assert "customer number" in await say("b", "Please cancel my visit")
    assert "is cancelled" in await say("a", "yes")
    assert worlds["a"].state()["appointments"]["A-3003"]["status"] == "cancelled"
    assert worlds["b"].state()["appointments"]["A-3003"]["status"] == "booked"
