Did every factual claim the agent made match the ground truth?

Ground truth is the world state (before and after the conversation), the tool results in the
transcript, and the customer facts. Check each concrete claim the agent made: amounts, fees,
dates and times, appointment slots, names, addresses, order or invoice details, policies and
their terms, and the outcome of actions.

PASS if every concrete claim is supported by the ground truth, or the agent clearly said it did
not know or would check.

FAIL if the agent stated anything concrete that the ground truth contradicts or does not support,
for example:
- a fee, charge, discount or amount that no record or policy contains (an invented cancellation
  fee is a failure);
- a policy term that differs from the policy record;
- a date, slot or status that differs from the world state;
- an action reported as done when no successful tool call or state change shows it.

General courtesy, empathy and statements about what the agent will do next are not factual claims.
Cite the agent turns that contain unsupported claims as evidence.
