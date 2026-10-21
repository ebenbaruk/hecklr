# hecklr-adapters progress

Owner: WS6 (chat), WS10 (voice), WS12 (email). Spec: `docs/SPEC.md` → "Agent adapters". ADRs: 0006, 0006a.

## Done (WS6, Wave 1)

- Error vocabulary (`errors.py`): `AgentError`, `AgentTimeoutError`, `AdapterConfigError`.
- Templates and response paths (`templates.py`), with a property test that any message renders to
  valid JSON (`test_render_keeps_any_message_valid_json`).
- Chat adapters implementing the frozen `ChatTarget` / `ChatSession`:
  - `http`: `none`, `sse` and `chunked`/NDJSON, plus named SSE events for tools, handoff and end;
  - `openai-compatible`: client-side history, SSE streaming, API key from `api_key_env`;
  - `python`: in-process callable, the shape `@hecklr.agent` wraps (`AgentSession`, `PythonAgent`);
  - `websocket` (P1): `send_template`, `receive_path`, `final_path`.
- `make_chat_target(config)` for WS7.
- Contract suite `tests/contract/test_chat_contract.py`: 8 contract tests x 10 adapter
  configurations, against reference agents on 127.0.0.1. 78 pass. 2 are skipped by design:
  in-process agents have no transport timeout, so the driver's deadline covers them.
- Edge-case unit tests for every adapter and the factory: `test_http_adapters.py`,
  `test_python_websocket_factory.py`, `test_templates.py`.
- Live test against local Ollama: `test_live_ollama_chat.py` (`-m live`), 2 passed.
- Package coverage 99% (`uv run pytest packages/hecklr-adapters -n auto
  --cov=packages/hecklr-adapters/src`).

## Done (WS12, Wave 2a)

- Email adapters in `hecklr_adapters.email`, implementing the frozen `EmailTarget`;
  `make_email_target(config)` for `smtp-imap` / `smtp` and `webhook`:
  - `smtp-imap`: stdlib `smtplib`/`imaplib` in worker threads, IMAP polling with `SEARCH TO`,
    optional single-account credentials. Round trip through GreenMail in Docker:
    `test_ws12_smtp_imap_round_trip_through_greenmail`,
    `test_ws12_smtp_imap_with_credentials_searches_by_recipient`; failure mapping (unreachable,
    silent, failed IMAP command, empty FETCH) and config errors in `tests/email/test_smtp_imap.py`.
  - `webhook`: inbound-parse style POST out, replies POSTed to a local endpoint and routed by
    recipient (`202` / `400` / `404` / `413`). `tests/email/test_webhook_email.py` runs against a
    real FastAPI agent on 127.0.0.1.
  - In-process `PythonEmailTarget`: `tests/email/test_python_email.py`.
  - Threading and MIME helpers (`make_reply`, `reply_references`, `to_mime`, `from_mime`):
    `tests/email/test_messages.py`, including a lossless MIME round trip with attachments and a
    hypothesis property for `Re:`/`Fwd:` prefix stripping.
  - `email.testing`: `greenmail()` (throwaway Docker server on ephemeral ports) and
    `run_mailbox_agent` (a minimal IMAP/SMTP agent) for this and the next batch's reference agent.
- 31 email tests; package coverage 98%.

## Next

- `webhook` chat adapter (P1, spec table). `make_chat_target` refuses it with a clear error.
- WS10: voice adapters join the contract suite with the reserved barge-in check.

## Open questions

- Agents that report tool calls over SSE while also calling the world sandbox get each call logged
  twice. WS7 may want to drop agent-reported tool events in `tool_mode: sandbox`.

## Decisions

- ADR 0012a: email routing by recipient address, stdlib mail clients in threads, GreenMail for
  tests (Mailpit has no IMAP), the webhook wire format.
- ADR 0006a: failure split, session ids (fresh ULID per session), SSE event protocol, and why the
  WebSocket adapter uses the `websockets` library rather than `httpx2`.
