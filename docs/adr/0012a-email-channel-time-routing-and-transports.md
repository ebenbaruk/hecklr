# 0012a. Email channel: time, reply routing, turns and transports

Date: 2026-10-21 · Status: accepted · Workstream: WS12

## Context

ADR 0006 froze `EmailTarget` (`deliver`, `replies`), `EmailMessage` and the `email` event, and
`hecklr-grade` already has `email_threading`. The spec leaves open how email time works ("simulate
time or run in real time with short timeouts?"), and says nothing about how replies are matched
to a trial, what a turn is when an agent sends two emails, or the wire format of the webhook
adapter. Fault F11 (replies without `In-Reply-To`) must be recorded faithfully, not lost.

## Decision

- **Time.** Waiting is real time, bounded by a per-reply deadline (`reply_timeout_s` from the
  adapter config, else `SessionContext.timeout_s`). Every email is stamped on the sink's clock: a
  customer email when it is handed to the agent, an agent email when it arrives. Reply time is
  the difference. Tests that need reply latency in minutes run the sink on a `FakeClock` that the
  test agent advances; no separate virtual-time scheduler exists.
- **Routing by address.** Each conversation writes from a fresh address
  `<persona>.<ulid>@<customer_domain>`. Adapters return mail addressed to the trial's customer
  addresses, and the driver keeps only mail to the current one. Threading headers are never used
  for routing, so an F11 agent is still heard and the grader sees exactly what it sent. A fresh
  address per attempt also keeps a retried trial away from the failed attempt's mail and agent
  session.
- **Turns.** Each email is a turn (`max_turns` counts both parties, ADR 0006a). The first agent
  email after a customer email ends the wait; any further agent emails already received (an
  acknowledgement, then the answer) are logged before the next customer email. They may push
  `DriverResult.turns` past the cap, because they really were sent. A `SimEnd.closing_text` is
  sent as a last email with no reply awaited: waiting would cost a full deadline whenever the
  agent rightly does not answer "thanks, bye".
- **Events.** The driver logs `email` events only (no duplicate utterances). The judges, the
  `contains` and `max_turns` graders and the HTML report already read them. Attachments are kept
  in full on the event, because a grader may need to check what was attached. The simulator reads
  each agent email as a person does: the new text without quoted history, plus one
  `[attachment: name (type)]` line per attachment.
- **Customer writing style.** The customer threads like a mail client (`In-Reply-To`,
  `References` = parent chain + parent, `Re:` subject) and quotes in one of `top_post`,
  `bottom_post` or `none`. `Persona` has no field for this, so the style is chosen per persona
  from `random.Random("hecklr-quote-style:<persona id>")`. It is stable across trials, with no
  schema change. `EmailDriver(quote_style=...)` forces one style.
- **smtp-imap.** It uses the standard library's `smtplib` and `imaplib` in worker threads, so the
  adapter adds no dependency and blocking I/O never runs on the event loop. IMAP is polled (1 s by
  default) with `SEARCH TO <address>`. With `username_env`/`password_env` it logs into one
  (catch-all) account. Without them, each customer address is its own mailbox, logged into with
  the address as user and password. That is the convention of local test servers with
  authentication disabled. Mail-server failures are the agent's (`AgentError`), like an
  unreachable HTTP agent. Tests use GreenMail in Docker (`hecklr_adapters.email.testing`). It
  serves SMTP and IMAP and creates mailboxes on demand. Mailpit has no IMAP.
- **webhook.** Hecklr POSTs `{"trial_id", "reply_url", "message": EmailMessage}` with the
  trial-id header. The agent POSTs `EmailMessage` JSON to `reply_url`
  (`/hecklr/email/replies`) and gets `202`, `400` or `404`. The reply endpoint is a
  standard-library HTTP server on its own thread. It starts on first use and stops on `close()`,
  because the Protocol has no lifecycle hooks. The webhook config names no inbox, so customer
  emails go to `agent@hecklr.test`.

## Consequences

- `hecklr run` needs one additive change in `compose.build_drivers`: map `Channel.EMAIL` to
  `make_email_driver(agent.adapters.email)` and drop it from `_UNAVAILABLE_CHANNELS`.
- Email latency statistics can be computed from event times alone. A latency grader for email
  is not needed for F11/F12 and is not built.
- Polling adds up to one poll interval to measured reply time over IMAP. This does not matter
  next to email latencies of minutes, and it is configurable (`poll_interval_s`).
