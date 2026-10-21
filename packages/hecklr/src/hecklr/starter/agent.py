"""A tiny rule-based support agent, so a fresh ``hecklr init`` project runs end to end.

Replace it with your real agent: keep the ``@hecklr.agent`` function, or point
``agent.adapters.chat`` in ``hecklr.yaml`` at your agent's HTTP endpoint instead. Its tools call
the world sandbox over HTTP (``POST {session.world_http_url}/tools/<name>``), the same way a real
agent's tool code would in test mode.
"""

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx2

import hecklr

_CUSTOMER_ID = re.compile(r"\bC-\d{4}\b", re.IGNORECASE)
_INTENTS = (
    ("reschedule", ("move", "reschedule", "change", "another day", "next week")),
    ("plan", ("plan", "subscription", "subscribed")),
    ("saturday", ("saturday", "weekend")),
    ("appointment", ("when", "appointment", "visit", "booked", "date")),
    ("goodbye", ("thank", "bye")),
)


@dataclass
class _Conversation:
    customer_id: str | None = None
    intent: str | None = None


# One entry per conversation; session ids are fresh for every trial, so nothing leaks between them.
_conversations: dict[str, _Conversation] = {}


@hecklr.agent(channel="chat")
async def reply(session: hecklr.Session, message: str) -> str:
    """Answer one customer message."""
    state = _conversations.setdefault(session.id, _Conversation())
    if found := _CUSTOMER_ID.search(message):
        state.customer_id = found.group().upper()
    lowered = message.lower()
    intent = next((name for name, words in _INTENTS if any(w in lowered for w in words)), None)
    state.intent = intent or state.intent
    if state.intent == "goodbye":
        return "You're welcome. Have a nice day!"
    if state.intent == "saturday":
        return "Yes, we do maintenance visits on Saturdays, from 9:00 to 13:00."
    if state.intent is None:
        return "Hello! I can tell you when your visit is, move it, or explain your plan."
    if state.customer_id is None:
        return "Happy to help. What is your customer id? It looks like C-1234."
    async with httpx2.AsyncClient(
        base_url=session.world_http_url or "", headers=session.headers, timeout=10
    ) as tools:
        return await _handle(tools, state.intent, state.customer_id)


async def _handle(tools: httpx2.AsyncClient, intent: str, customer_id: str) -> str:
    if intent == "plan":
        customer = await tools.post("/tools/get_customer", json={"id": customer_id})
        if customer.status_code != 200:
            return f"I could not find customer {customer_id}. Could you check the id?"
        return f"You are on our {customer.json()['plan']} maintenance plan."
    appointment = await tools.post("/tools/get_appointment", json={"customer_id": customer_id})
    if appointment.status_code != 200:
        return f"I could not find a visit for customer {customer_id}. Could you check the id?"
    visit = appointment.json()
    if intent != "reschedule":
        return f"Your {visit['service']} visit is booked for {visit['slot']}."
    new_slot = (datetime.fromisoformat(visit["slot"]) + timedelta(days=7)).replace(hour=9)
    slot = new_slot.strftime("%Y-%m-%dT%H:%M")
    moved = await tools.post(
        "/tools/reschedule_appointment", json={"customer_id": customer_id, "slot": slot}
    )
    if moved.status_code != 200:
        return "Sorry, I could not move your visit right now. Please try again later."
    return f"Done: your {visit['service']} visit is moved to {slot}."
