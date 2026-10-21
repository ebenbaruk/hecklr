# F2: the reschedule tool description swaps the date format to MM/DD

**Change.** In `tools/descriptions.yaml`, the `slot` parameter of `reschedule_appointment`:

```diff
-        slot: { type: string, description: "New slot start, format YYYY-MM-DDTHH:MM, e.g. 2026-10-27T09:00." }
+        slot: { type: string, description: "New slot start, format MM/DD/YYYY HH:MM, e.g. 10/27/2026 09:00." }
```

**Effect.** The agent writes rescheduled slots as `10/26/2026 09:00`. The backend stores the
string as given (like many real backends), so the visit's slot is no longer a valid date-time
while the agent tells the customer it was moved.

**Must be caught by** a state assertion (spec table): every reschedule scenario checks
`world.appointments[...].slot`, and a non-ISO slot fails it (ADR 0004a: malformed data fails the
assertion). Booking is unaffected; `book_appointment` keeps its own ISO description.
