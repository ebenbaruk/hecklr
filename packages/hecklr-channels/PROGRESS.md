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

## Done (WS12, Wave 2a)

- `EmailDriver` (`email/driver.py`) implements the frozen `ChannelDriver` for email over any
  `EmailTarget`; `make_email_driver(config)` binds it to an `smtp-imap` or `webhook` adapter.
  - Fresh customer address per conversation, so replies are routed by recipient and retries never
    see a failed attempt's mail or agent session:
    `test_ws12_each_run_writes_from_a_fresh_address`,
    `test_ws12_mail_to_another_address_is_not_part_of_the_conversation`.
  - The customer threads like a mail client and quotes in the persona's style (`email/quoting.py`):
    `test_ws12_customer_emails_thread_like_a_mail_client`,
    `test_ws12_quote_styles_place_the_new_text_where_each_client_does`,
    `test_ws12_a_persona_keeps_one_quote_style_and_personas_differ`.
  - Reply time on the simulated clock (7 minutes on a `FakeClock`):
    `test_ws12_email_reply_time_is_measured_on_the_simulated_clock`.
  - Threading headers and attachments are recorded verbatim; the simulator reads new text plus
    attachment notes: `test_ws12_the_simulator_reads_new_text_and_attachments_not_quoted_history`.
  - Limits, deadlines, closing email, multiple replies, agent and harness failures: 26 tests in
    `tests/email/test_email_driver.py`.
  - F11 caught by `email_threading` (hecklr-grade, already built by WS4):
    `test_sc8_f11_broken_threading_agent_is_caught`.
  - End to end over real transports, clean agent passes and F11 agent fails threading:
    `test_ws12_email_over_smtp_imap_is_threaded_and_graded` (GreenMail in Docker, skipped without
    Docker) and `test_ws12_email_over_webhook_is_threaded_and_graded` (127.0.0.1 agent).
  - FakeLLM customer (hecklr-sim guided mode) by email:
    `test_ws12_email_end_to_end_with_a_fake_llm_customer`.
- 41 email tests; package coverage 99% (`email/driver.py` 98%).

## Next

- Integrator: wire email into `hecklr run`. In `packages/hecklr/src/hecklr/compose.py`, remove
  `Channel.EMAIL` from `_UNAVAILABLE_CHANNELS`, and in `build_drivers` add
  `drivers[Channel.EMAIL] = make_email_driver(email)` when `project.config.agent.adapters.email`
  is set (import `make_email_driver` from `hecklr_channels`).
- Next batch: the reference agent's email part and the F11/F12 variants run through this driver.

## Open questions

- None.

## Decisions

- ADR 0012a: real-time waits with stamps on the sink's clock, routing by address, each email a
  turn, closing email not awaited, per-persona quoting style from a seeded RNG.
- ADR 0006a:
  - `max_turns` counts both parties, and a user turn starts only if its reply fits;
  - running out of duration mid-reply is a `timeout` with `end_reason=max_duration`;
  - an empty reply has `latency_ms` unset.
