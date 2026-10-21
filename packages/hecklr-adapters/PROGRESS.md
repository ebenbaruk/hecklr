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

## Next

- `webhook` chat adapter (P1, spec table). `make_chat_target` refuses it with a clear error.
- WS11: the voice reference agent (Pipecat) and F7-F10 use these adapters; wire
  `make_voice_target` into `hecklr run` (compose) once RFC 0010a is decided.
- `webrtc` (WHIP, P1) and `sip` (P2) voice adapters.

## Open questions

- Agents that report tool calls over SSE while also calling the world sandbox get each call logged
  twice. WS7 may want to drop agent-reported tool events in `tool_mode: sandbox`.
- Pipecat SmallWebRTC reference bot (Pipecat 1.13): it hears ~110 ms less of each caller
  utterance, and one reply late in a long call arrives ~150 ms later than the rest. The same
  caller track looped through plain aiortc keeps every edge within 10 ms
  (`test_caller_audio_crosses_webrtc_with_its_speech_edges_in_place`), so it is the bot's receive
  path; the case carries wider allowances. Tracked in
  [#27](https://github.com/ebenbaruk/hecklr/issues/27). Run them with
  `uv run pytest packages/hecklr-adapters/tests/contract/test_voice_contract.py`.
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
- `pipecat-ai[webrtc,websocket]` is a dev dependency (real Pipecat in the contract); resolving it
  moves the workspace to onnxruntime 1.24 and protobuf 6 (Pipecat's pins). WS11 needs Pipecat too.
