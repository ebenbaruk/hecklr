# F12: a tool timeout is not handled, so the agent goes silent

**Change.** `mailer.yaml`, the email front end's settings (`examples/acme-email`), drops the
deadline on world tool calls:

```diff
-tool_timeout_s: 5
+tool_timeout_s: null
```

**Effect.** When a world tool hangs, the clean agent gives up after 5 seconds and the brain handles
the failure (it tells a customer in an emergency to call the 24/7 emergency line, or completes a
cancellation without the policy text it could not read). Under F12 the call is awaited forever, so
the brain never answers: the customer's email gets no reply at all.

**Must be caught by** dead air and `resolved` (spec table). On email, dead air is the email that
is never answered: the driver's reply deadline (`timeout_s` of the webhook adapter,
`reply_timeout_s` of `smtp-imap`) runs out and the trial ends as a `timeout`, which the report
counts as a failed trial (cause `timeout`). The `resolved` judge fails a conversation that "went
silent". The scenarios that inject a hung tool (`faults: [{kind: timeout}]` on `get_policy` or
`create_emergency_ticket`) carry the tag `catches-f12`; the others never meet a hung tool and are
unaffected (`tests/test_suite.py::test_sc8_f12_fails_exactly_the_tool_timeout_scenarios`).

**Why the email agent.** The fault lives in the agent's tool layer, so it is channel-agnostic in
nature, but WS12 owns it on email, the only acme front end whose tool deadline is a setting in a
file an overlay can replace. The chat agent puts its deadline on its HTTP client
(`ACME_TOOL_TIMEOUT_S`, `examples/acme-maintenance`), outside the overlay mechanism; see
`docs/adr/0012b-email-reference-agent-and-f11-f12.md`.

```bash
uv run python examples/acme-email/agent/serve.py --fault F12 webhook --port 8100
```
