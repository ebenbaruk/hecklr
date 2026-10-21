# hecklr-channels

Hecklr channel drivers for chat and email conversations.

Part of [Hecklr](../../README.md). Owned by WS6 (chat) and WS12 (email); see `docs/SPEC.md`
("Channel drivers") and ADRs 0006 and 0006a.

## Chat

```python
from hecklr_adapters import make_chat_target
from hecklr_channels import ChatDriver

driver = ChatDriver(make_chat_target(config.agent.adapters.chat))  # a ChannelDriver
result = await driver.run(ctx, brief, simulator, sink)  # DriverResult
```

The driver alternates simulator turns and agent replies and writes every utterance, streamed tool
call, tool result, handoff and error to the sink. Agent utterances carry ADR 0006 timing:
`Event.t_ms` is the first token, `end_ms` the last, and `latency_ms` is time to first token. Time
to last token is `latency_ms + end_ms - t_ms`.

| Situation | `status` | `end_reason` |
| --- | --- | --- |
| Customer ends (`SimEnd`) | `ok` | the simulator's reason |
| Agent hands off / closes the chat | `ok` | `handoff` / `agent_ended` |
| `max_turns` or `max_duration_s` reached between turns | `ok` | `max_turns` / `max_duration` |
| Time runs out mid-reply | `timeout` | `max_duration` |
| Reply misses `SessionContext.timeout_s`, or the adapter times out | `timeout` | `error` |
| Adapter raises `AgentError` | `agent_error` | `error` |
| Simulator or sink fails | raised (harness error); the session is still closed | |

One driver instance can serve many concurrent trials.

## Email

```python
from hecklr_channels import make_email_driver

driver = make_email_driver(config.agent.adapters.email)  # smtp-imap or webhook
result = await driver.run(ctx, brief, simulator, sink)
```

`EmailDriver(target, agent_addr=...)` wraps any `EmailTarget`, e.g. the in-process
`hecklr_adapters.email.PythonEmailTarget`. Each conversation writes from a fresh customer address,
threads like a mail client (`In-Reply-To`, `References`, `Re:` subject) and quotes in the persona's
style (`top_post`, `bottom_post` or `none`). Every customer and agent email is logged as an
`email` event, headers and attachments included, for the `email_threading` grader. Reply time is
the agent email's `t_ms` minus the customer email's (ADR 0012a).

| Situation | `status` | `end_reason` |
| --- | --- | --- |
| Customer ends (`SimEnd`); a closing email is sent, no reply awaited | `ok` | the simulator's reason |
| `max_turns` or `max_duration_s` reached between emails | `ok` | `max_turns` / `max_duration` |
| No reply within `reply_timeout_s` (default `SessionContext.timeout_s`) | `timeout` | `error` |
| Scenario time runs out awaiting, or before, a reply | `timeout` | `max_duration` |
| Delivery or the reply stream raises `AgentError` | `agent_error` | `error` |
| Simulator, sink or adapter bug | raised (harness error); the mailbox is still released | |

To try the smtp-imap adapter by hand, start a local GreenMail with
`docker compose -f tests/fixtures/email/compose.yaml up -d` (SMTP 1025, IMAP 1143, the adapter
defaults).
