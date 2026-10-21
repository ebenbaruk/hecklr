# 0006a. Chat driver and adapter semantics: failure split, limits, wire protocols

Date: 2026-10-21 · Status: accepted · Workstream: WS6

## Context

ADR 0006 froze the chat contracts (`ChatTarget`, `ChatSession`, `ChannelDriver`, `DriverResult`)
and the utterance timing fields. It left open the parts a driver and its adapters must agree on:

- how a driver tells an agent failure (reported in the trial) from a harness failure (raised and
  retried);
- what `ScenarioLimits.max_turns` counts;
- how an agent behind plain HTTP reports tool calls, handoffs and the end of a conversation.

## Decision

- **Failure split.** Adapters raise `hecklr_adapters.AgentError` when the agent fails: it is
  unreachable, returns an HTTP error status, sends malformed data, or (in process) raises.
  `AgentTimeoutError` is the subclass for a stalled agent. The driver maps `AgentError` to
  `agent_error`, and `AgentTimeoutError` or a missed deadline to `timeout`. Each is also logged as
  an `error` event (`actor=system`, `source=agent`). Any other exception is a harness failure and
  propagates. The session is still closed, shielded from cancellation. Bad configuration raises
  `AdapterConfigError` when the target is built, before any trial runs.
  `hecklr-channels` therefore depends on `hecklr-adapters`.
- **Limits.**
  - `max_turns` counts both parties' turns, the same unit as `DriverResult.turns` and
    `UtterancePayload.turn`. A user turn starts only if its reply also fits, so a conversation
    never ends on an unanswered message and never exceeds the cap.
  - `max_duration_s` runs from the start of `ChatDriver.run`, on the sink's clock.
  - Reaching a limit between turns ends the trial normally (`ok`, `max_turns` / `max_duration`).
  - Running out of duration mid-reply gives `timeout` + `max_duration`.
  - Missing the per-turn deadline `SessionContext.timeout_s` gives `timeout` + `error`.
  - A `SimEnd.closing_text` is sent, and its reply collected, only if the limits still allow it.
- **Timing edge cases.** A reply with no text has no first token: it is logged when the turn
  ends, with `latency_ms` unset, so it never enters TTFT statistics. On a failure, the partial
  reply received so far is logged before the error.
- **Agent-reported events.** Streamed `tool_call` / `tool_result` / `handoff` chunks become events
  (`agent` / `tool` / `agent`) as they arrive. Over the `http` adapter they travel as named SSE
  events (`event: tool_call` etc. with the `hecklr_schemas` payload as JSON data); `event: end`
  means the agent closed the conversation. An agent pointed at the world sandbox should not report
  its tool calls this way too, or they are logged twice.
- **Sessions.** Each adapter session gets a fresh ULID as its session id (`{{session_id}}`), not
  the trial id, so a retried trial never meets the memory of a failed attempt. The trial id
  travels separately in `X-Hecklr-Trial-Id` and `{{trial_id}}`.
- **Transports.** HTTP uses `httpx2` (its built-in SSE parser), one client per session. WebSocket
  uses the `websockets` asyncio client. `httpx2`'s WebSocket session keeps an anyio task group
  open for the whole connection, which cannot span the separate `open` / `send` / `close` calls of
  the Protocol. The `websocket` adapter therefore runs on asyncio only, which is the harness's
  backend anyway.
- **Contract suite.** Reference agents run on 127.0.0.1 ephemeral ports. Only those tests get
  sockets, through pytest-socket's `enable_socket` + `allow_hosts(["127.0.0.1"])` markers.

## Consequences

- An adapter bug that raises something other than `AgentError` surfaces as a harness error. It is
  retried and shown, never silently charged to the agent.
- Graders can read handoffs and agent-ended conversations from `DriverResult.end_reason` and
  `handoff` events on any adapter that can signal them: `python`, `http` with SSE.
- The `webhook` chat adapter (P1) is not built yet. `make_chat_target` refuses it with a clear
  error.
