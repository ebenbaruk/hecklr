# 0007e. Voice and email in `hecklr run`: drivers, speech providers, recordings, world URLs

Date: 2026-10-22 · Status: accepted · Workstream: WS-wire (Wave 2b)

## Context

Wave 2a shipped the voice engine (`VoiceDriver`, ADR 0009a), the voice transports (ADR 0010a),
the email driver and adapters (ADR 0012a), but `hecklr.compose` still refused voice and email
suites. Composing them needs choices the spec leaves open: which TTS and ASR a run uses when
`hecklr.yaml` says nothing, how a missing extra or key surfaces, where recordings go, and how a
voice or email agent learns its trial's world sandbox (chat agents do since ADR 0007d; the voice
and email adapters sent only the trial id). `hecklr.yaml` also could not name the `websocket`
voice adapter (RFC 0010a).

## Decision

- **Drivers.** `build_drivers` binds `VoiceDriver(make_voice_target(...))` and
  `EmailDriver(make_email_target(...))` (`hecklr/channels.py`) for the channels a suite uses.
  `hecklr-voice` stays an optional extra (`hecklr[voice]`); a voice suite without it is a
  configuration error naming the install command, exit 78.
- **Speech providers, checked before any trial.** TTS is per persona (`voice.tts.provider`), the
  impairment chain from the persona's voice profile, ASR from `models.asr`. When `models.asr` is
  unset the caller hears with `faster-whisper/tiny.en` (local, free; 4.5% WER clean, ADR 0009a's
  measurement), from the `hecklr[local-voice]` extra. `fake/*` ASR and the `synthetic` TTS are
  accepted when named: they are what tests and calibration use, never a silent default. Cloud
  TTS/ASR need their API key. `prepare_run` builds the ASR and every voice persona's TTS and
  impairment chain once, so a missing extra, key, ffmpeg codec or unknown noise id fails before
  the run is recorded (`ChannelUnavailableError`, exit 78), with a hint when the default ASR was
  the cause. VAD stays the engine's default energy VAD (ADR 0009a); `hecklr.yaml` has no VAD field
  and Silero is opt-in in code. Local models live in `<output_dir>/models`, synthesized caller
  speech is cached in `<output_dir>/cache/tts`.
- **Recordings** go to the run's own directory (`<output_dir>/runs/<run id>/audio/...`, ADR
  0009a's layout), so the HTML report written there plays them with waveforms and turn markers,
  and a copied run directory stays self-contained. Drivers are therefore built after planning,
  when the run id exists.
- **Resources.** The email webhook's reply endpoint starts on first use and must stop when the
  run ends, or a second run in one process (`calibrate --aa`, tests) cannot bind its port.
  `PreparedRun.releases` holds such callbacks; `execute_run` calls them in a `finally`. The email
  driver is built in `hecklr/channels.py` rather than with `hecklr_channels.make_email_driver`
  because the latter keeps its target private.
- **World URLs on every channel.** `session_fields(ctx)` (`trial_id`, `world_url`,
  `world_mcp_url`; world fields only when the trial has a sandbox) goes where a transport carries
  data the agent reads: the WebSocket voice `start` message (`pcm`), Twilio
  `start.customParameters`, Pipecat SmallWebRTC `request_data`, LiveKit participant metadata
  (JSON) and attributes (`hecklr.trial_id`, `hecklr.world_url`, `hecklr.world_mcp_url`), and the
  email webhook body. `session_headers(ctx)` goes on every handshake or request with headers: the
  WebSocket voice handshake, the Pipecat offer POST, the email webhook POST, and each SMTP email
  (`X-Hecklr-World-Url` on the message). The names match the chat templates' `{{world_url}}`.
- **RFC 0010a is accepted;** until the schema carries the `websocket` voice member,
  `hecklr.config` widens `agent.adapters.voice` by that member (subclasses of the schema models;
  the instances are still `HecklrConfig`), and every CLI command loads `hecklr.yaml` through it.
- **`hecklr calibrate --voice-loopback`** runs the SC4 loopback (in-process agent, real
  `VoiceDriver`) at 300/600/900 ms reply delay and 150/300 ms barge-in reaction, and prints each
  timing error with a 95% interval; it fails when any measurement is off by more than 20 ms.

## Consequences

- `hecklr run --channels voice,email` works end to end: proven over a real WebSocket voice agent
  (latency within 20 ms of the agent's injected delay, recordings in the report) and over the
  email webhook and SMTP/IMAP (GreenMail in Docker), with each agent finding its trial's world
  from what the adapter sent (`tests/test_compose_channels.py`).
- A first voice run without `models.asr` downloads faster-whisper `tiny.en` (~75 MB) into the
  output directory; CI keeps `models.asr: fake/asr` and synthetic voices.
- Removing the RFC 0010a shim is a two-file change once the schema has the member.
- Voice agents written for Hecklr should read `world_url` from the transport's metadata; email
  agents from the webhook body or the `X-Hecklr-World-Url` header.
