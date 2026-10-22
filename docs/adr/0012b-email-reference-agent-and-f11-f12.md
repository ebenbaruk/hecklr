# 0012b. Email reference agent: a front end over the acme brains, and faults F11/F12 as mailer settings

Date: 2026-10-22 · Status: proposed · Workstream: WS12b

## Context

SC8 needs the reference agent reachable by email and two email faults that Hecklr must catch:
F11 (replies lose the thread; caught by `email_threading`) and F12 (a tool timeout is not handled
and the agent goes silent; caught by dead air and `resolved`). The chat agent
(`examples/acme-maintenance`, ADR 0008a) already has two brains that read the same editable files,
and its faults are one-line overlays of those files. The overlay loader refuses any other file.
Neither F11 nor F12 is about what the agent says: F11 is mail plumbing, and F12 is how the tool
layer waits. The spec's "dead air" is a voice metric (`VoiceMetric.DEAD_AIR`); email has no
silence events.

## Decision

- **A front end, not a new agent.** `examples/acme-email/agent/acme_email` imports the chat
  agent's `AcmeAgent` (both brains, its files, its tool backends) and adds only mail: one
  conversation per customer address, the brain reads the new text of each email (quoted history
  stripped with `hecklr_channels.email.visible_text`), replies come from the address the customer
  wrote to with `In-Reply-To`, `References` and a `Re:` subject (`make_reply`). No acme-maintenance
  file changes.
- **Three transports.** Webhook (FastAPI: `202` at once, the reply POSTed to `reply_url` from a
  background task, as inbound-parse providers expect), mailbox (IMAP `UNSEEN` polling, fetching
  marks mail seen; SMTP replies; blocking `imaplib`/`smtplib` in worker threads; each email in its
  own task, so a reply stuck on a hung tool never holds up other customers) and in process
  (`PythonEmailTarget`) for tests.
- **The trial's world, per email**: the webhook body's `world_url`, the `X-Hecklr-World-Url`
  header (HTTP on the webhook, MIME on mail), `ACME_WORLD_BASE_URL` + the trial id
  (`X-Hecklr-Trial-Id` header or body), else a local copy; the same order as the chat agent
  (ADR 0007d). These are the fields and headers WS-wire's email adapters send.
- **The front end has one file of settings, `mailer.yaml`**: `thread_replies` and
  `tool_timeout_s`. F11 and F12 are one-line overlays of it (`thread_replies: false`,
  `tool_timeout_s: null`), so ADR 0008a's rule holds: a fault changes one line of one file and no
  code path. The email loader takes an overlay with `mailer.yaml` (and a README) for the mailer
  and the clean brain files, and passes any other overlay to the chat loader. The chat agent
  refuses F11/F12 overlays, so an email fault cannot run silently as a clean chat agent.
- **Tool deadlines belong to the front end.** Every tool call runs under `DeadlineTools`
  (`anyio.move_on_after`); a missed deadline is a `timeout` error the brain handles like any tool
  failure. The HTTP client has no timeout of its own, so the deadline is one setting.
  `tool_timeout_s: null` awaits a hung tool forever: the brain never answers and no reply is sent.
- **F12 on email; dead air on email is the unanswered email.** The driver's reply deadline expires,
  the trial ends `timeout`, which the report counts as a failed trial (cause `timeout`), and the
  `resolved` judge fails a conversation that went silent. Scenarios that inject a hung tool
  (`faults: [{kind: timeout}]` on `get_policy` during a cancellation, or `create_emergency_ticket`
  during an emergency) are tagged `catches-f12`; the clean agent passes them (it cancels without
  the policy text, or sends the customer to the 24/7 emergency line). The fault is channel-agnostic
  in nature, but it is not added to the chat agent: its deadline is its HTTP client's
  (`ACME_TOOL_TIMEOUT_S`), outside the overlay mechanism, and making it overlayable would change
  acme-maintenance's loader. The voice agent (WS11) can reuse `DeadlineTools` and this overlay if
  it wants F12 as voice dead air.
- **Two scenario sets**, as for chat: `scripted/` (14, suite `smoke`: scripted customers,
  deterministic graders, a free exact matrix with the rules brain) and `scenarios/` (13 guided,
  suite `ci`: luna customers and judges). Every scenario lists `email_threading` (tag
  `catches-f11`); each fault has at least 6 scenarios in each set, so a fully flipped fault has a
  paired sign-flip p of at most 2 / 2^6.

## Consequences

- The scripted matrix is exact in process: the clean agent passes 14/14, F11 fails only
  `email_threading` on all 14, F12 fails exactly the 6 `catches-f12` scenarios, each as a
  `timeout` (`tests/test_suite.py`). Webhook + real sandbox and GreenMail SMTP/IMAP tests repeat
  the key cases over the real transports.
- An F12 trial costs a full reply deadline of wall time (`timeout_s` 45 s in `hecklr.yaml`), the
  price of observing silence in real time (ADR 0012a).
- After an F12 trial ends, the sandbox releases the hung call, the brain answers late, and the
  reply POST gets `404` (no live conversation): the agent logs it and `/healthz` counts it in
  `replies_failed`.
- The email agent depends on `hecklr_channels` for quote stripping and `hecklr_adapters` for
  threading and MIME helpers; a real agent would carry its own, but the reference agent stays
  short and the helpers are public API.
