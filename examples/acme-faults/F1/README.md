# F1: the prompt drops "confirm the slot before booking"

**Change.** `prompts/system.md` replaces the rule with its opposite:

```diff
-- Before booking or rescheduling, offer slots that match the customer's preferences and get their explicit confirmation of the exact slot.
+- To save the customer time, book or reschedule the first free slot find_slots returns as soon as the customer is verified; do not offer alternatives or ask the customer to confirm.
```

Deleting the rule alone was invisible on `gpt-6-luna`, which offers slots and asks for a yes by
default (M1 fault matrix, Tier B): a fault must change behaviour to be a fault.

**Effect.** The agent books or moves a visit to the first free slot as soon as it has verified
the customer, without matching the customer's preferences or asking for a yes. A customer who
wants "next week, mornings" gets Thursday 22 October at 09:00.

**Must be caught by** `tool_called` + the `resolved` judge (spec table). The options-first
scenarios (`scenarios/scheduling/*-options-*.yaml`) let the customer name a slot only after the
agent offers some, so an agent that books without asking books the wrong slot. The scheduling
scenarios also assert the booked slot with a `state` criterion, and the `resolved` rubric fails
"booked a slot the customer did not agree to".
