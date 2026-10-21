# hecklr-adapters progress

Owner: WS6 (chat), WS10 (voice). Spec: `docs/SPEC.md` → "Agent adapters". ADRs: 0006, 0006a.

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

## Done (WS7d, Wave 2a)

- World URL forwarding (ADR 0007d): `hecklr_adapters.context.session_headers(ctx)` adds
  `X-Hecklr-World-Url` / `X-Hecklr-World-Mcp-Url` (exported as `WORLD_URL_HEADER`,
  `WORLD_MCP_URL_HEADER`) to every `http`, `openai-compatible` and `websocket` request; request
  templates accept `{{world_url}}` and `{{world_mcp_url}}` (empty without a sandbox).
  `tests/test_world_forwarding.py` (8 tests: headers in `none`/`sse`/`chunked`, body placeholders,
  no-world case, openai-compatible headers, websocket template). 146 tests, 99% coverage.
- Proof: `hecklr run --suite smoke` over the `http` adapter against the reference agent (rules
  brain): 18/33 before (all 15 booking, cancel and reschedule scenarios failed), 33/33 after.

## Next

- `webhook` chat adapter (P1, spec table). `make_chat_target` refuses it with a clear error.
- WS10: voice adapters join the contract suite with the reserved barge-in check.

## Open questions

- Agents that report tool calls over SSE while also calling the world sandbox get each call logged
  twice. WS7 may want to drop agent-reported tool events in `tool_mode: sandbox`.

## Decisions

- ADR 0006a: failure split, session ids (fresh ULID per session), SSE event protocol, and why the
  WebSocket adapter uses the `websockets` library rather than `httpx2`.
