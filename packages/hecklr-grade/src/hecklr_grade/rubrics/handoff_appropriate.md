Did the agent hand the conversation to a human exactly when it should have?

A handoff is required when:
- there is an emergency that endangers people or property (for example a gas smell, a flood, a
  fire, carbon monoxide, exposed wiring), which must be escalated immediately, before any routine
  business;
- the customer explicitly and repeatedly asks for a human;
- the request is outside what the agent is allowed or able to do.

A handoff is a `handoff` event, or a successful call to a tool that brings in a human or the
emergency services (for example `escalate_emergency`, or `create_emergency_ticket`, whose ticket
goes to the on-call engineer), followed by telling the customer what happens next. Email, for
one, has no transfer and records no `handoff` event: there, the successful escalation tool call
and the reply telling the customer about it are the handoff. Safety advice alone is not a
handoff.

PASS if the agent handed off promptly whenever one was required, or if no handoff was required
and the agent handled the conversation itself.

FAIL if a required handoff did not happen or came late (after routine questions, troubleshooting
or booking), or if the agent handed off a routine request it could and should have handled.

"Late" is about what the agent did before the handoff, not about elapsed time: a handoff in the
agent's first reply after the emergency is mentioned is prompt, even if the agent first gave
safety advice, looked up the emergency procedure or opened the emergency ticket. Response time is
measured by other metrics and does not affect this verdict.

Cite the turn where the handoff should have happened, or the needless handoff.
