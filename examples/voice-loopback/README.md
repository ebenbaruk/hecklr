# voice-loopback: the networked calibration agent (SC3, SC4)

A voice agent whose timing is known exactly. It speaks the `pcm` protocol of Hecklr's
`websocket` voice adapter (JSON `start`, binary 16 kHz 16-bit PCM both ways, JSON `stop`) and:

- answers every caller turn `--reply-after-ms` after the caller's **last speech sample**, once
  the caller has been quiet for `--endpoint-ms`;
- stops talking `--barge-in-reaction-ms` after the **first speech sample** of a caller who talks
  over it (`none`: never stops, like fault F8).

It places caller audio on the caller's media clock (earliest-arrival alignment, like an RTP
receiver), so network and scheduling delays do not move its truth, and sends its reply as 20 ms
packets in real time, each up to 40 ms ahead (its send buffer). Hecklr must measure both timings
within 20 ms (spec, "Calibration and self-tests"). The in-process twin used by `hecklr-voice`'s
own tests is `hecklr_voice.testing.LoopbackVoiceTarget`; this one runs in its own process and
crosses a real socket.

```bash
# the agent
uv run python examples/voice-loopback/serve.py --port 8765 --reply-after-ms 600 \
    --barge-in-reaction-ms 200
curl http://127.0.0.1:8765/healthz   # settings, calls, CPU, the agent's own worst send lateness

# SC4: 300/600/900 ms with barge-ins, every measurement within 20 ms (exit 1 otherwise)
uv run python examples/voice-loopback/sc4_calibrate.py --calls 10 --json sc4.json

# SC3: N simultaneous calls from one harness; underruns, loop lag, errors, CPU
uv run python examples/voice-loopback/sc3_load.py --calls 50 100 150 --json sc3.json
```

From `hecklr.yaml` (once the `websocket` voice adapter is selectable, RFC 0010a):

```yaml
agent:
  adapters:
    voice: { type: websocket, url: "ws://127.0.0.1:8765/", protocol: pcm }
```

| Module | What it is |
| --- | --- |
| `loopback_agent/call.py` | `LoopbackCall`: the timing decisions for one call, transport-free |
| `loopback_agent/server.py` | `LoopbackServer`: the WebSocket shell, paced sending, `/healthz` |
| `loopback_agent/bench.py` | Calls through Hecklr's `VoiceDriver` + `websocket` adapter; error summaries with bootstrap intervals |
| `sc4_calibrate.py`, `sc3_load.py` | The SC4 and SC3 runs |

Tests: `uv run pytest examples/voice-loopback` (the SC4 test starts the agent as a subprocess and
calls it over 127.0.0.1). Results are in [`../acme-voice/RESULTS.md`](../acme-voice/RESULTS.md).
