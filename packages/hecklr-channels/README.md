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
