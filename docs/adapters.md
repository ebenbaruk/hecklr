# Connect your agent

An **adapter** is the only code that knows how to reach your agent. You pick one per channel under
`agent.adapters` in `hecklr.yaml`; everything else in Hecklr talks to three small protocols
(`ChatTarget`, `VoiceTarget`, `EmailTarget`). Every adapter passes the same contract test suite
against a reference agent before it ships: connect, multi-turn memory, streaming, trial-id
propagation, session isolation, cancellation, timeouts and agent errors (and for voice: barge-in
and the ±20 ms timing check).

Each section below has a configuration and a runnable reference agent from the repository's
`examples/` directory, so you can see the whole round trip before wiring up your own.

## Chat

### `http`: JSON over HTTP, optionally streamed

```yaml
agent:
  adapters:
    chat:
      type: http
      url: http://localhost:8000/chat/stream
      stream: sse                # none | sse | chunked
      request_template: { message: "{{message}}", session_id: "{{session_id}}" }   # the default
      response_path: reply       # where the text is in a JSON reply; the whole body if omitted
      timeout_s: 60
```

Hecklr POSTs every customer turn; your agent keeps the conversation by `session_id` (fresh for
every trial). Placeholders: `{{message}}`, `{{session_id}}`, `{{trial_id}}`, `{{world_url}}`,
`{{world_mcp_url}}`. With `stream: sse`, unnamed events are text deltas, and an agent can also
report what it did with named events whose data is the matching Hecklr payload as JSON:
`tool_call`, `tool_result`, `handoff`, and `end` when it closes the conversation.

A minimal agent, in FastAPI, whose tool calls go to the trial's world sandbox:

```python
import httpx
from fastapi import FastAPI, Header
from pydantic import BaseModel

app = FastAPI()


class Turn(BaseModel):
    message: str
    session_id: str


@app.post("/chat")
async def chat(turn: Turn, x_hecklr_world_url: str | None = Header(default=None)) -> dict[str, str]:
    # In tests, tools act on the trial's world; in production, on your real backend.
    async with httpx.AsyncClient(base_url=x_hecklr_world_url or "https://api.example.com") as tools:
        visit = (await tools.post("/tools/get_appointment", json={"customer_id": "C-1002"})).json()
    return {"reply": f"Your visit is booked for {visit['slot']}."}
```

**Working example:** `examples/acme-maintenance` (the reference agent):

```bash
uv run python examples/acme-maintenance/agent/serve.py --brain rules --port 8000
cd examples/acme-maintenance && uv run hecklr run --suite smoke
```

### `openai-compatible`: any `/v1/chat/completions` endpoint

```yaml
agent:
  adapters:
    chat: { type: openai-compatible, url: "http://localhost:8000/v1", model: support-agent, api_key_env: AGENT_API_KEY }
```

For agents behind an OpenAI-compatible proxy or server. Hecklr keeps the history and sends the
whole conversation every turn; `stream: true` (the default) reads SSE. `hecklr init --agent-url`
detects these servers from `/v1/models`.

### `python`: in process

```python
import hecklr


@hecklr.agent(channel="chat")
async def reply(session: hecklr.Session, message: str) -> str:
    return await support_bot.answer(session.id, message)  # session.world_http_url for tools
```

```yaml
agent:
  adapters:
    chat: { type: python, entrypoint: "my_bot:reply" }
```

The callable may also be an async generator of text chunks, to stream. **Working example:** the
starter agent `hecklr init` writes (`hecklr_agent.py`, used by the [quickstart](quickstart.md)).

### `websocket`: one socket per conversation

```yaml
agent:
  adapters:
    chat:
      type: websocket
      url: ws://localhost:8000/ws
      send_template: { type: user_message, text: "{{message}}" }
      receive_path: delta        # dotted path to the text in each agent message
      final_path: done           # dotted path to a boolean marking the end of the agent's turn
```

## Voice

Every voice adapter plays the agent's audio out in real time, as a phone would, and timestamps
every frame on arrival on the harness clock: the latency Hecklr reports is the latency a caller
hears. Install the voice engine with `pip install "hecklr[voice]"` (and `hecklr[local-voice]` for
local Kokoro text-to-speech, faster-whisper and Silero VAD). See the [voice guide](guides/voice.md).

### `websocket`: raw PCM, Twilio Media Streams, or Pipecat frames

Most production voice stacks speak one of these over a WebSocket:

```yaml
agent:
  adapters:
    voice: { type: websocket, url: "ws://localhost:8765/", protocol: pcm }   # or twilio, pipecat
```

| `protocol` | On the wire | Barge-in signal |
| --- | --- | --- |
| `pcm` | binary 16-bit mono PCM at `sample_rate` (16 kHz default) both ways; a JSON `start` message with `trial_id`, `world_url`, `world_mcp_url`; `stop` at hang-up | the agent goes quiet or sends `{"type": "clear"}` |
| `twilio` | Twilio Media Streams: `connected`, `start` (ids and world URLs in `customParameters`), `media` (8 kHz μ-law), `stop`; your `mark`s are echoed when played | `clear` |
| `pipecat` | Pipecat's `ProtobufFrameSerializer` frames (`FastAPIWebsocketTransport`) | `InterruptionFrame` |

**Working example:** `examples/acme-voice` (VAD, faster-whisper, the acme brain, Kokoro or
synthetic speech, over `pcm`):

```bash
uv run python examples/acme-voice/agent/serve.py --brain rules --tts synthetic --vad energy --port 8765
uv run hecklr run --config examples/acme-voice/hecklr.yaml --suite voice-timing --budget-usd 0
```

### `pipecat`: Pipecat SmallWebRTC

```yaml
agent:
  adapters:
    voice: { type: pipecat, url: "http://localhost:7860/api/offer" }
```

Hecklr posts a WebRTC offer to your Pipecat runner's offer endpoint and joins as the caller (needs
`pip install "hecklr[voice]"`, which brings aiortc). The trial id and world URLs arrive as headers
and in `request_data` (Pipecat's `runner_args.body`). The contract suite runs it against a real
Pipecat pipeline.

### `livekit`: join a room

```yaml
agent:
  adapters:
    voice: { type: livekit, url: "ws://localhost:7880", room_prefix: hecklr }
```

Every trial joins its own room `<room_prefix>-<trial id>` with a token signed from
`LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` (names configurable with `api_key_env` /
`api_secret_env`). Your agent finds the trial id and world URLs in the caller's participant
metadata and attributes (`hecklr.trial_id`, `hecklr.world_url`, `hecklr.world_mcp_url`). The
contract suite runs against `livekit-server` in Docker.

!!! warning "Not built yet"
    The voice adapters `webrtc` (generic WHIP-style offer/answer) and `sip`, and the chat `webhook`
    adapter, are part of the configuration schema but not implemented yet: `hecklr run` refuses
    them with a configuration error that says so. See the [roadmap](roadmap.md).

## Email

### `webhook`: HTTP in, HTTP out

```yaml
agent:
  adapters:
    email: { type: webhook, url: "http://localhost:8100/email", listen_port: 8787, timeout_s: 120 }
```

Hecklr POSTs every customer email to `url` as `{"trial_id", "world_url", "world_mcp_url",
"reply_url", "message"}`, and your agent POSTs its reply (an email message as JSON) to `reply_url`.
`timeout_s` is the reply deadline: an agent that stays silent that long fails the trial.

### `smtp-imap`: a real mailbox

```yaml
agent:
  adapters:
    email: { type: smtp-imap, inbox: support@acme.test, smtp_port: 1025, imap_port: 1143 }
```

Hecklr sends each customer email over SMTP to `inbox` from a fresh customer address, with the
`X-Hecklr-Trial-Id` and world-URL headers, and collects replies over IMAP. Replies are matched by
recipient address, never by threading headers, so an agent that loses the thread is still recorded
exactly as it wrote, and failed by the `email_threading` grader. For local tests, start GreenMail
with `docker compose -f tests/fixtures/email/compose.yaml up -d`.

**Working example:** `examples/acme-email`, the reference agent by email, both ways:

```bash
uv run python examples/acme-email/agent/serve.py --brain rules webhook --port 8100
uv run hecklr run -c examples/acme-email/hecklr.yaml -s smoke --channels email --budget-usd 0
```

## One agent, three channels

A scenario runs on every channel in its `channels` list that `agent.adapters` configures; narrow a
run with `--channels voice,email`.

```yaml title="hecklr.yaml"
version: 1
agent:
  name: acme-support
  adapters:
    chat: { type: http, url: "http://localhost:8000/chat/stream", stream: sse }
    voice: { type: websocket, url: "ws://localhost:8765/", protocol: pcm }
    email: { type: webhook, url: "http://localhost:8100/email", listen_port: 8787 }
world: worlds/acme-maintenance.yaml
suites:
  ci: { scenarios: "scenarios/**/*.yaml", tags: [core], trials: 3 }
models:
  simulator: openai/gpt-6-luna
  judge: openai/gpt-6-luna
  asr: faster-whisper/base.en       # the caller's ears on the voice channel
```

## Authenticating to your agent

The `http`, `websocket` (chat and voice) and `webhook` adapters send `headers` with every request
or handshake. Keep secrets out of `hecklr.yaml` with `headers_env`, which maps a header name to the
environment variable holding its value:

```yaml
agent:
  adapters:
    chat:
      type: http
      url: https://staging.example.com/chat
      headers: { X-Team: qa }                         # literal values, safe to commit
      headers_env: { Authorization: AGENT_AUTH_HEADER }
```

```bash
export AGENT_AUTH_HEADER="Bearer $(cat ~/.agent-token)"
```

Each variable is read once, when the run starts. An unset or empty variable stops the run before
any trial with an error naming the variable (never its value), and a header set in both `headers`
and `headers_env` is refused rather than silently resolved. The value stays in the adapter: it is
not written to the run record, events, logs or reports. The `openai-compatible` adapter keeps its
own `api_key_env`.

## Writing a new adapter

Implement the protocol for your channel (`hecklr_schemas.ChatTarget`, `VoiceTarget` or
`EmailTarget`), add a case to `packages/hecklr-adapters/tests/contract/conftest.py`, and make the
contract suite pass. See [CONTRIBUTING](https://github.com/ebenbaruk/hecklr/blob/main/CONTRIBUTING.md).
