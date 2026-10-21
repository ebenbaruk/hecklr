# hecklr-channels progress

Owner: WS6 (chat), WS12 (email). Spec: `docs/SPEC.md` → "Channel drivers". ADRs: 0006, 0006a.

## Done (WS6, Wave 1)

- `ChatDriver` (`chat.py`) implements the frozen `ChannelDriver` for chat:
  - ADR 0006 timing: `test_ws6_chat_timing_ttft_and_ttlt_follow_adr_0006` (FakeClock). Measured
    TTFT and TTLT match the agent's own stamps within 20 ms over the real python and HTTP SSE
    adapters: `test_ws6_chat_*_adapter_latency_matches_injected_delay`.
  - Streamed `tool_call` / `tool_result` / `handoff` chunks are logged as events.
  - Limits (`max_turns`, `max_duration_s`), the per-turn deadline, `SimEnd` closing text, and
    agent-ended / handoff ends.
  - Failure mapping: `AgentError` gives `agent_error`, deadlines give `timeout`, and simulator or
    sink failures are raised with the session still closed (also under cancellation).
  - End to end with a FakeLLM-backed customer: `test_ws6_chat_end_to_end_with_a_fake_llm_customer`.
- 28 tests, chat.py coverage 99%. Live: a real Ollama-vs-Ollama conversation
  (`test_live_ollama_conversation.py`, `-m live`) passed.

## Next

- WS12: the email driver goes in its own module (`email.py`) beside `chat.py`.

## Open questions

- None.

## Decisions

- ADR 0006a:
  - `max_turns` counts both parties, and a user turn starts only if its reply fits;
  - running out of duration mid-reply is a `timeout` with `end_reason=max_duration`;
  - an empty reply has `latency_ms` unset.
