# F1: the prompt drops "confirm the slot before booking"

**Change.** `prompts/system.md` loses one rule:

```diff
-- Before booking or rescheduling, offer slots that match the customer's preferences and get their explicit confirmation of the exact slot.
```

**Effect.** The agent books or moves a visit to the first free slot as soon as it has verified
the customer, without matching the customer's preferences or asking for a yes. A customer who
wants "next week, mornings" gets Thursday 22 October at 09:00.

**Must be caught by** `tool_called` + the `resolved` judge (spec table). The scheduling
scenarios also assert the booked slot with a `state` criterion, and the `resolved` rubric fails
"booked a slot the customer did not agree to".
