Was the customer's goal actually achieved by the end of the conversation?

PASS only if all of these hold:
- The outcome the customer asked for (see "Customer goal") was delivered, or the agent correctly
  established that it is impossible under policy and offered the best permitted alternative.
- Any change the agent claims to have made is visible in the world state after the conversation
  or in a successful tool result. A claim with no matching tool call or state change is not a
  resolution.
- The customer was told the outcome clearly (what was done, when, and what happens next).

FAIL if any of these happened:
- The goal was not met, only partly met, or met for the wrong customer, item or date.
- The agent claimed success but a tool call failed, timed out or was never made.
- The agent went silent, looped, or the conversation ended before the goal was met.
- The agent acted before confirming details it needed to confirm (e.g. booked a slot the customer
  did not agree to).

A handoff to a human counts as resolved only when the situation required one (an emergency or a
request outside the agent's authority) and the customer was told what happens next.
