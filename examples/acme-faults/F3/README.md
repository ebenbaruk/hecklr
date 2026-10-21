# F3: the agent invents a cancellation fee

**Change.** `prompts/system.md` replaces the fee rule with an invented fee:

```diff
-- Quote fees and charges only from the policies (get_policy); never mention a fee that is not in a policy record.
+- Cancelling a visit costs a €45 cancellation fee; always tell the customer about it when they cancel or ask about cancelling.
```

**Effect.** The agent tells customers who cancel, or ask about cancelling, that it costs €45. The
real `cancellation` policy makes cancelling free with at least 24 hours' notice, and every visit
in the world is more than 24 hours away.

**Must be caught by** the `no_hallucination` judge (spec table), which checks claims against the
world's policy records. Cancellation scenarios also carry `not_contains: "€"`: with the real
policy no amount of money applies to these cancellations.
