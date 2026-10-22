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
| `smtp-imap` (alias `smtp`) | SMTP to `inbox`, with `X-Hecklr-Trial-Id`, `X-Hecklr-World-Url`, `X-Hecklr-World-Mcp-Url` | IMAP polling, `SEARCH TO` each customer address of the trial |
| `webhook` | `POST url` `{"trial_id", "world_url", "world_mcp_url", "reply_url", "message"}` with the same headers | the agent POSTs an `EmailMessage` to `reply_url` (`202` / `400` / `404`) |
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

## Voice adapters

Build one from the `agent.adapters.voice` entry of `hecklr.yaml` (or a
`WebSocketVoiceAdapterConfig`, until RFC 0010a adds it to the schema):

```python
from hecklr_adapters import make_voice_target

target = make_voice_target(config.agent.adapters.voice)  # a hecklr_schemas.VoiceTarget
link = await target.connect(ctx)  # an AudioLink
await link.send_frame(pcm_20ms_16khz, 16_000)  # caller audio, paced by the driver
async for frame in link.frames():  # agent audio: 16 kHz TimedFrames
    ...
await link.hangup()
```

| `type` | Reaches | Extra | Barge-in |
| --- | --- | --- | --- |
| `websocket`, `protocol: pcm` | binary PCM16 at `sample_rate` both ways; JSON `start` (with `trial_id`, `world_url`, `world_mcp_url`) / `stop` from Hecklr, `{"type": "clear"}` from the agent | none | agent goes quiet or sends `clear` |
| `websocket`, `protocol: twilio` | Twilio Media Streams: `connected`, `start` (`trial_id`, `world_url`, `world_mcp_url` in `customParameters`), `media` (8 kHz mu-law), `stop`; the agent's `mark`s are echoed when played or cleared | none | `clear` |
| `websocket`, `protocol: pipecat` | Pipecat's `ProtobufFrameSerializer` frames (`FastAPIWebsocketTransport`) | none | `InterruptionFrame` |
| `pipecat` | Pipecat SmallWebRTC: offer POSTed to `url` (the runner's `/api/offer`) | `pipecat` (aiortc) | Pipecat drops its queued audio |
| `livekit` | joins room `<room_prefix>-<trial_id>`, token signed with `api_key_env` / `api_secret_env` | `livekit` | the agent clears its source |

Every adapter plays the agent's audio out in real time, as a phone would (a burst is heard over
its length; `clear` cuts it where playback had got to), and delivers 16 kHz frames whose
`sample_index` is the time they were heard (ADR 0010a).

### The trial's world, on every channel

An agent must call its own trial's world sandbox, or the trial's state assertions grade a world
it never touched (ADRs 0007d and 0007e). Every adapter tells the agent the trial id and, when the
trial has a sandbox, its world URLs (`world_url`: the OpenAPI base, tools at
`POST <world_url>/tools/<name>`; `world_mcp_url`: the MCP endpoint):

| Transport | Where the agent reads them |
| --- | --- |
| `http`, `openai-compatible`, `websocket` (chat) | headers `X-Hecklr-Trial-Id`, `X-Hecklr-World-Url`, `X-Hecklr-World-Mcp-Url`; templates `{{world_url}}`, `{{world_mcp_url}}` |
| `websocket` (voice) | the same headers on the handshake; `pcm`: the `start` message; `twilio`: `start.customParameters` |
| `pipecat` | the same headers on the offer POST, and `request_data` (Pipecat's `runner_args.body`) |
| `livekit` | the caller's participant metadata (JSON) and attributes `hecklr.trial_id`, `hecklr.world_url`, `hecklr.world_mcp_url` |
| email `webhook` | the same headers, and `trial_id`, `world_url`, `world_mcp_url` in the POSTed body |
| email `smtp-imap` | the same headers on each customer email |

`hecklr_adapters.session_headers(ctx)` and `session_fields(ctx)` build them.

### Voice contract suite

`tests/contract/test_voice_contract.py` drives every voice adapter with the real `VoiceDriver`
against a loopback reference agent with known timing (`voice_reference.py`): connect, caller
audio at the agent's rate, real-time 16 kHz agent audio, SC4 (response latency and barge-in
reaction within 20 ms of what the agent actually did), agent hang-up and failure, a silent agent,
clean hang-up, and no event-loop stalls. Cases: WebSocket raw PCM (16 kHz paced, 24 kHz burst),
Twilio, Pipecat protobuf; Pipecat SmallWebRTC against a real Pipecat pipeline; LiveKit against
`livekit-server` in Docker (skipped, with the reason, when Docker is unavailable).
