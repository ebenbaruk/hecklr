# hecklr-adapters

Hecklr target adapters and the shared adapter contract test suite.

Part of [Hecklr](../../README.md). Owned by WS6 (chat), WS10 (voice) and WS12 (email); see
`docs/SPEC.md` ("Agent adapters") and ADRs 0006a and 0012a.

## Chat adapters

Build one from the `agent.adapters.chat` entry of `hecklr.yaml`:

```python
from hecklr_adapters import make_chat_target

target = make_chat_target(config.agent.adapters.chat)  # a hecklr_schemas.ChatTarget
session = await target.open(ctx)  # ctx: SessionContext
async for chunk in session.send("Hi, I need to move my visit"):
    ...  # AgentChunk deltas, then one final chunk
summary = await session.close()
```

| `type` | Reaches | Streaming | Memory |
| --- | --- | --- | --- |
| `http` | `POST url` with `request_template` (`{{message}}`, `{{session_id}}`, `{{trial_id}}`); reply at `response_path` | `none`, `sse`, `chunked` (raw text, or NDJSON with `response_path`) | agent side, keyed by `session_id` |
| `openai-compatible` | `POST {url}/chat/completions` | SSE when `stream: true` | client side: full history every turn |
| `python` | `module:function`, `async (session, message) -> str` or an async iterator of `str` / `AgentChunk` | when the callable yields | the agent's own, keyed by `session.id` |
| `websocket` | one socket per conversation, `send_template` out, `receive_path` / `final_path` in | until `final_path` is true | agent side, per connection |

Every request carries `X-Hecklr-Trial-Id` (`SessionContext.headers()`) plus the configured
`headers`. Over `http` with `stream: sse`, an agent can also report what it did with named events
whose data is the `hecklr_schemas` payload as JSON: `tool_call`, `tool_result`, `handoff`, and
`end` when it closes the conversation.

Errors: `AgentError` / `AgentTimeoutError` when the agent fails (the driver reports the trial as
`agent_error` / `timeout`), `AdapterConfigError` when the configuration cannot work (raised by
`make_chat_target`). Anything else is a harness bug and propagates.

## Email adapters

Build one from the `agent.adapters.email` entry of `hecklr.yaml` with `make_email_target`
(`hecklr_channels.make_email_driver` wraps it in the email driver). Each implements
`hecklr_schemas.EmailTarget`: `deliver(msg, ctx)` and `replies(ctx)`.

| `type` | Out | In |
| --- | --- | --- |
| `smtp-imap` (alias `smtp`) | SMTP to `inbox`, with `X-Hecklr-Trial-Id` | IMAP polling, `SEARCH TO` each customer address of the trial |
| `webhook` | `POST url` `{"trial_id", "reply_url", "message"}` | the agent POSTs an `EmailMessage` to `reply_url` (`202` / `400` / `404`) |
| in-process | `PythonEmailTarget(agent)`, `async (session, msg) -> EmailMessage \| list \| None` | queued per trial |

Replies are routed by recipient address, never by threading headers, so an agent that loses the
thread (F11) is still recorded exactly as it wrote. `make_reply(parent, body=..., from_addr=...)`
builds an RFC 5322 threaded reply; `to_mime` / `from_mime` convert to and from the wire.
`hecklr_adapters.email.testing` starts a throwaway GreenMail server in Docker (`greenmail()`) and
runs a minimal IMAP/SMTP agent (`run_mailbox_agent`). Decisions: ADR 0012a.

## Contract suite

`tests/contract/` runs one parameterized suite against every chat adapter: connect, multi-turn
memory, streaming shape, trial-id propagation, session isolation, cancellation, network timeout
and agent errors. The reference agents run on 127.0.0.1 ephemeral ports. A new adapter is added by
giving it a case in `tests/contract/conftest.py`; it must pass the suite before merge.
