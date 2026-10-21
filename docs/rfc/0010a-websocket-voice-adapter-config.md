# RFC 0010a. A `websocket` member for `VoiceAdapterConfig`

Date: 2026-10-21 · Status: proposed · Workstream: WS10 · Needs: architect approval

## Problem

`hecklr_schemas.VoiceAdapterConfig` lists `pipecat` (SmallWebRTC), `livekit`, `webrtc` and
`sip`. Most production voice stacks are also, or only, reachable over a WebSocket: Twilio Media
Streams and the telephony serializers built on it (Pipecat's `TwilioFrameSerializer`, Telnyx,
Plivo, Exotel), Pipecat's own `FastAPIWebsocketTransport`, and custom raw-PCM sockets. WS10
builds that adapter as the P0 transport, but `hecklr.yaml` cannot name it, so `hecklr run`
cannot select it from configuration.

## Proposed change

Add to `hecklr_schemas/config.py`, unchanged from the model WS10 ships in
`hecklr_adapters.voice.config`:

```python
class WebSocketVoiceAdapterConfig(FrozenModel):
    type: Literal["websocket"]
    url: Annotated[str, Field(pattern=r"^wss?://")]
    protocol: Literal["pcm", "twilio", "pipecat"] = "pcm"
    sample_rate: Annotated[int, Field(ge=8_000, le=48_000)] | None = None  # twilio: always 8 kHz
    headers: dict[str, str] = Field(default_factory=dict)
    connect_timeout_s: Annotated[float, Field(gt=0)] = 10.0
```

and make it a member of the `VoiceAdapterConfig` discriminated union. `type: websocket` already
names the chat WebSocket adapter in `ChatAdapterConfig`; the unions are separate, so the same
discriminator value is unambiguous (`agent.adapters.voice.type` vs `agent.adapters.chat.type`).
The JSON Schema export gains one definition. No existing file changes meaning.

## Alternatives

- **A separate `twilio` type.** Twilio framing is one option of a WebSocket transport, as is
  Pipecat's protobuf framing; three near-identical types would triple the documentation.
- **Reuse `WebSocketAdapterConfig` (chat).** Its fields (templates, JSON paths) mean nothing for
  audio, and it lacks protocol and sample rate.

## Migration

None. Until approved, `hecklr_adapters.make_voice_target` accepts the WS10 model directly, and
the integrator can wire it into `hecklr run` once the schema carries it; afterwards the adapter
imports the schema's model and the local copy is deleted.
