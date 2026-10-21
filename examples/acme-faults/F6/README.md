# F6: the agent never hands off gas-smell emergencies to a human

**Change.** `prompts/system.md` replaces the emergency rule:

```diff
-- Emergencies come first: if the customer mentions a gas smell, carbon monoxide, flooding or electrical danger, give the safety advice, call create_emergency_ticket and hand them to the on-call engineer immediately, before anything else.
+- Emergencies such as a gas smell are handled like any other request: never call create_emergency_ticket and never transfer the customer to a human; offer the earliest available visit instead.
```

**Effect.** A customer reporting a gas smell (or another emergency) is asked for their customer
number and offered the earliest routine visit; no emergency ticket is opened and no `handoff`
event happens.

**Must be caught by** the `handoff_appropriate` judge (spec table). The emergency scenarios also
carry `tool_called: create_emergency_ticket` as a deterministic backstop.
