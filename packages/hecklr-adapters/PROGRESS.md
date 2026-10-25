# hecklr-adapters progress

Owner: WS6 (chat), WS10 (voice), WS12 (email). Spec: `docs/SPEC.md` → "Agent adapters". ADRs:
0006, 0006a, 0009a, 0010a. RFC: 0010a (websocket voice config, proposed).

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

## Done (WS10, Wave 2a): voice transports

- `voice/audio.py`: G.711 mu-law (bit-exact against `hecklr-voice`'s reference codec for all
  65,536 samples), `StreamResampler` (windowed sinc at exact rational positions, cached phase
  kernels; delay = 8-sample look-ahead, < 1.6 ms; chunking never changes the output, a
  hypothesis test), `ToHarness`.
- `voice/playout.py`: `Playout` (real-time playout, arrival placement, 40 ms rejoin, `clear`,
  Twilio marks) and `Receiver` (background reading, errors raised after the audio before them).
- `voice/websocket.py` + `voice/wire.py`: the `websocket` adapter with `pcm` (any rate),
  `twilio` (Media Streams, mu-law 8 kHz, marks and clear) and `pipecat` (protobuf frames,
  interop-tested against Pipecat's `ProtobufFrameSerializer` and `TwilioFrameSerializer`).
- `voice/pipecat.py`: SmallWebRTC offer/answer client on aiortc (extra `pipecat`).
- `voice/livekit.py`: room participant with a self-signed token (extra `livekit`).
- `voice/factory.py`: `make_voice_target` (refuses `webrtc` / `sip` clearly; a missing extra or
  LiveKit credential is an `AdapterConfigError`).
- Voice contract `tests/contract/test_voice_contract.py`: 9 tests x 6 cases, driven by the real
  `VoiceDriver` against a loopback agent on its own thread (`voice_reference.py`), Pipecat
  pipeline (`voice_pipecat.py`), LiveKit worker + `livekit-server` v1.13.10 in Docker
  (`voice_livekit.py`). SC4 measured offsets (agent's actual timing vs Hecklr's measurement):
  WebSocket pcm 16 kHz 0-12 ms, pcm 24 kHz burst 1-18 ms, Twilio 2-18 ms, Pipecat protobuf
  1-12 ms; LiveKit 80-170 ms transport delay idle (up to ~340 ms on a loaded machine), spread
  < 60 ms; Pipecat SmallWebRTC ~210 ms. WebRTC SC4 tests skip under xdist (load-sensitive) and
  run serially. CI runs the suite only under xdist, so **SC4 over LiveKit and SmallWebRTC is
  verified manually** (`uv run pytest packages/hecklr-adapters/tests/contract/test_voice_contract.py`
  on an idle machine with Docker); WebSocket SC4 runs in CI. A serial CI step needs Docker and
  an idle runner and belongs with the CI owner.
- Unit tests `tests/voice/`: audio, playout, wire, websocket, pipecat, livekit, factory.
- Review round 1: hang-up always releases the transport (socket, peer connection, room, reading
  task) even when the goodbye fails or is cancelled (`test_hangup_releases_the_socket_when_the_goodbye_fails`,
  `test_a_cancelled_hangup_still_releases_the_socket`,
  `test_hangup_closes_the_peer_connection_even_when_a_step_fails`,
  `test_hangup_leaves_the_room_even_when_a_step_fails`); contract case
  `test_ws10_contract_target_places_a_new_call_after_a_failed_one` (all 6 transports); the
  LiveKit reference agent leaves its rooms when cancelled; Pipecat negotiation uses
  `ctx.timeout_s`; `TimedFrame.t_ms` origin recorded in ADR 0010a.
- Package coverage 97% (`uv run pytest packages/hecklr-adapters -n auto
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

## Done (WS-wire, Wave 2b)

- World URLs on every voice and email transport (ADR 0007e): `session_fields(ctx)` in the pcm
  `start` message, Twilio `customParameters`, Pipecat `request_data`, LiveKit metadata and
  `hecklr.*` attributes, the email webhook body; `session_headers(ctx)` on the WebSocket voice
  handshake, the Pipecat offer, the email webhook and SMTP mail.
  `tests/test_world_forwarding_voice_email.py` (10 tests).
- Issue #28: the caller-audio contract test judges the heard speech span only where the case's
  timing is meaningful (LiveKit times speech by arrival); the rate check runs everywhere.

## Done (hflake, Wave 5): flaky tests fixed at the root (ADR 00hfa)

- #73 (1) SmallWebRTC teardown: hang-up waits for ICE gathering, cancels every aioice
  connectivity check and stops every ICE transport; loopback agents are called without a STUN
  server (local connect 5 s -> ms, no internet traffic from unit tests); max-bundle.
  `test_issue73_hangup_leaves_no_ice_check_retrying_on_a_closed_socket`,
  `test_issue73_hangup_during_gathering_leaves_no_stun_query_behind` (both fail on the old code
  with the exact `'NoneType' object has no attribute 'sendto'` ExceptionGroup),
  `test_issue73_only_an_agent_off_this_machine_is_called_with_a_stun_server`.
- #73 (2) the loop-free contract times each event-loop step's CPU (bound unchanged, 5 ms per
  step); worst steps measured 0.2-2.0 ms across the six cases.
  `test_ws10_loop_step_timer_catches_a_step_that_holds_the_loop`.
- #56 `WebSocketChatTarget(config, connect_timeout_s=10.0)`: the handshake has its own bound.
  `test_issue56_the_reply_timeout_does_not_bound_the_opening_handshake`,
  `test_issue56_a_handshake_slower_than_the_connect_timeout_is_an_agent_timeout`,
  `test_issue56_the_connect_timeout_must_be_positive`.
- #70 IMAP reply polling retries a dropped poll once the mailbox has answered.
  `test_issue70_a_poll_that_fails_once_the_mailbox_answered_is_retried`,
  `test_issue70_polls_failing_for_the_whole_timeout_fail_the_reply_stream`; end to end in
  `examples/acme-email` (`test_issue70_dropped_reply_polls_change_no_verdict`).
- #27 explained: the "lost" caller speech is aiortc holding a stream's last five frames until
  more follow (the contract now keeps the line open; `caller_loss_ms` removed, the case is held
  to 20 ms); the late reply is the in-process bot's Pipecat sender catching up in a burst after
  being starved (`jitter_ms=200` kept). `test_issue27_aiortc_holds_the_last_five_frames_...`,
  `tests/contract/test_pipecat_bot.py`.

## Done (WShrun, Wave 6-close)

- **#115 `headers_env`** (RFC 0023, accepted): `resolve_headers` reads each variable once when an
  `http`, `websocket` (chat and voice) or `webhook` adapter is built; an unset or empty variable,
  or a header set in both `headers` and `headers_env`, is an `AdapterConfigError` naming the
  variable, never the value. Tests: `tests/test_headers_env.py` (`test_issue115_*`, 8, including
  a real WebSocket handshake on 127.0.0.1). Real: acme `smoke` with `headers_env` exits 78 naming
  the variable when it is unset and completes 33/33 when set; the value is in no run artifact.
- **#119** an undecodable or unstorable agent email (lone surrogate, unknown charset,
  `unicode-escape` body) is an `AgentError`: `test_issue119_*` (4) in `tests/email/`.

## Next

- `webhook` chat adapter (P1, spec table). `make_chat_target` refuses it with a clear error.
- WS11: the voice reference agent (Pipecat) and F7-F10 use these adapters; wire
  `make_voice_target` into `hecklr run` (compose) once RFC 0010a is decided.
- `webrtc` (WHIP, P1) and `sip` (P2) voice adapters.

## Open questions

- Agents that report tool calls over SSE while also calling the world sandbox get each call logged
  twice. WS7 may want to drop agent-reported tool events in `tool_mode: sandbox`.
- The SmallWebRTC case's 200 ms `jitter_ms` allowance could shrink only if the reference bot
  ran in its own process (GIL) on an idle CPU, or if the playout compressed silence after a burst
  as a NetEq jitter buffer does (ADR 00hfa). Neither is needed for any SC.
- `WebSocketAdapterConfig` has no `connect_timeout_s` (frozen schema): an RFC can add it if a user
  needs to set the chat handshake bound from `hecklr.yaml`.
- An async function-scoped fixture that started the DSP pool (`warm_dsp_pool`) left the next
  test with dead worker processes (`BrokenWorkerProcess`); the driver warms the pool itself, so
  the fixture was dropped. Worth a look by WS9 if a long-lived harness warms the pool from a
  different task or loop than the one that uses it.

## Decisions

- ADR 0012a: email routing by recipient address, stdlib mail clients in threads, GreenMail for
  tests (Mailpit has no IMAP), the webhook wire format.
- ADR 0006a: failure split, session ids (fresh ULID per session), SSE event protocol, and why the
  WebSocket adapter uses the `websockets` library rather than `httpx2`.
- ADR 0010a: playout clock, exact resampling, background reading, LiveKit backlog handling,
  Pipecat as SmallWebRTC, and how the contract states truth and transport delay.
- RFC 0010a: add `WebSocketVoiceAdapterConfig` to the schema's `VoiceAdapterConfig`.
- ADR 00hfa: SmallWebRTC teardown and STUN policy, per-step loop-free contract, the chat
  handshake's own timeout, IMAP poll retries, and what the Pipecat bot's allowances measure.
- `pipecat-ai[webrtc,websocket]` is a dev dependency (real Pipecat in the contract); resolving it
  moves the workspace to onnxruntime 1.24 and protobuf 6 (Pipecat's pins). WS11 needs Pipecat too.
