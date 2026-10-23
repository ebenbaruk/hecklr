# demo/: the 90-second demo, reproducible

The spec's demo ("The 90-second demo") from real runs, with the numbers recorded as-is in
[RESULTS.md](RESULTS.md). Everything here runs locally and, except the optional judged run, costs
nothing.

| File | What it does |
| --- | --- |
| `chat_gate.sh` | Copies the reference agent, records a clean baseline, changes **one line** of its system prompt, re-runs and gates: `REGRESSION, −27.3 pts [−42.4, −12.1], p = 0.004, 9 scenarios flipped`. Outputs in `out/chat/` |
| `voice_call.py` | Calls the reference voice agent with callers on a noisy phone line who barge in, for the clean agent and for fault F8 (barge-in disabled), and compares them. Outputs in `out/voice/` |
| `record_gif.sh`, `demo.tape` | Records `docs/assets/demo.gif` with [VHS](https://github.com/charmbracelet/vhs) from a real run |
| `screenshots.py` | Screenshots of the gate report, a voice trial, and the local UI into `docs/assets/` |
| `VIDEO.md` | Shot list and script for the narrated 90-second video (needs a human) |
| `RESULTS.md` | The measured numbers, with run ids |

```bash
uv sync --all-packages --all-extras
demo/chat_gate.sh                                   # about 40 s
uv run python demo/voice_call.py                    # a few minutes; local speech models download once
brew install vhs && demo/record_gif.sh              # the README GIF
uv run --script demo/screenshots.py                 # report screenshots (uses your installed Chrome)
cd demo/out/voice/acme-voice && uv run hecklr ui --no-open --port 8484 &
uv run --script demo/screenshots.py --ui http://127.0.0.1:8484   # UI screenshots too
```

The UI screenshots need the UI built into the package (`cd ui && npm ci && npm run build`); that
build output is not committed.

`hecklr fix` (the demo's fifth beat) is still in development, so the demo stops at the report.
