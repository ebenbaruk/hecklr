# Demo results (recorded as-is)

Every number in the README, the docs and the launch drafts that comes from the demo is copied
from these runs, measured on 2026-10-23 on an Apple-silicon laptop (10 cores) that other work was
also using. Run ids are ULIDs from the runs' own records.

## 1. One-line prompt edit, gated (chat)

`demo/chat_gate.sh` · reference agent `acme-maintenance`, rules brain, scripted `smoke` suite
(33 scenarios × 1 trial), deterministic graders · **$0**.

The edit (`prompts/system.md`, line 11; the same change as seeded fault F1):

```diff
-- Before booking or rescheduling, offer slots that match the customer's preferences and get their explicit confirmation of the exact slot.
+- To save the customer time, book or reschedule the first free slot find_slots returns as soon as the customer is verified; do not offer alternatives or ask the customer to confirm.
```

| | Run | Pass rate (95% CI) |
| --- | --- | --- |
| Baseline (clean) | `01M5K49XTKVANH66JQFM5TCA73` | 100.0% [89.6%, 100.0%] |
| Candidate (edited) | `01M5K4A6Y4FRWVW2YZ2Y54PWQG` | 72.7% [55.8%, 84.9%] |

`hecklr gate --suite smoke` → **REGRESSION, exit 1**:

```text
Pass rate 100.0% [89.6%, 100.0%] → 72.7% [55.8%, 84.9%]: −27.3 pts [−42.4, −12.1] (95% CI), p=0.004
· 33 scenarios, 33 vs 33 trials · δ=3 pts, α=0.05
Flipped pass → fail: 9 scenarios
- state: the visit moved to the slot asked for (6): scripted-reschedule-1 ... -6
- state: the visit is booked at the slot asked for (3): scripted-booking-1 ... -3
Slices regressed (q < α): channel=chat −27.3 pts [−42.4, −12.1], tag=catches-f1 −100.0 pts
```

The rules brain is deterministic, so every re-run gives the same verdict, Δ, interval and flips
(the run ids and latencies change). Latency p95 moved by a few ms either way, "inconclusive".

### With real judges

The same edited agent on the `matrix` suite (the 33 scripted scenarios plus 18 judge-only twins),
judges on `openai/gpt-6-luna`: run `01M5K47GCW4ZKPFXDTRXTHCJPT`, pass rate 70.6% [57.0%, 81.3%],
**spend $0.0183** (the pre-run estimate printed "at most $0.01"; see the note below). Every
`resolved` judgment on a booking or rescheduling twin failed, with rationales such as:

> The agent booked Thursday 22 October at 09:00 instead of the requested Wednesday 28 October
> afternoon, despite the requested slot being available. The booking was made without the
> customer agreeing to this different date and time, and the agent clearly confirmed the
> incorrect booking. (`judged-booking-1`, `judge:resolved`)

The `no_hallucination` and `handoff_appropriate` judges passed on every cancellation and emergency
twin, as they should: the edit does not touch fees or emergencies.

Reproduce: `demo/chat_gate.sh setup`, then in `demo/out/chat/acme`
`uv run --env-file <your .env with OPENAI_API_KEY> hecklr run --suite matrix --budget-usd 0.08`, then
`demo/chat_gate.sh teardown`. (This run's store was replaced by a later `chat_gate.sh`; its
numbers are recorded here.)

## 2. A caller on a noisy line interrupts (voice)

`uv run python demo/voice_call.py` · reference voice agent `acme-voice` (rules brain, Kokoro voice,
Silero VAD, faster-whisper `base.en`), 8 barge-in scenarios × 1 trial. Callers: Kokoro voice
`am_michael`, street noise at 15 dB SNR (reported in the 10 dB bucket), G.711 μ-law phone line,
barge-in on every agent turn. Hecklr hears the agent with faster-whisper `base.en` · **$0**.

| | Clean `01M5K3JCCTWX9APRZH3JSFA3Z1` | F8, barge-in disabled `01M5K3N6DXM9NM2YA8S00VT1VN` |
| --- | --- | --- |
| Pass rate | 87.5% [52.9%, 97.8%] | 0.0% [0.0%, 32.4%] |
| Barge-in reaction p95 | **212 ms** [190, 218] | **2,731 ms** [1,872, 2,731] |
| Response latency p50 | 865 ms [847, 902] | 792 ms [666, 888] |
| Response latency p95 | 1,456 ms [1,027, 1,702] | 1,485 ms [1,023, 1,485] |
| Talk-over | 1.04 s/min [0.68, 1.37] | 8.27 s/min [4.48, 12.82] |

`hecklr compare` → **REGRESSION**: −87.5 pts [−100.0, −62.5], p = 0.016; 7 scenarios flipped,
all on `voice:barge_in_reaction` ("p95 1831 ms over 3 event(s); must be below 400").

The clean agent's one failure, `voice-barge-in-electrician`, is not an agent error: its turns were
too short for the caller to barge into, so the only criterion was skipped and the trial has no
passing criterion.

In the clean run's trial `voice-barge-in-book` (the screenshot `docs/assets/report-voice-trial.png`
and `docs/assets/ui-trial.png`), the caller barged in three times and the agent stopped talking
168, 154 and 152 ms later, each time measured from the caller's audio.

## 3. The instrument itself: loopback calibration (SC4)

`hecklr calibrate --voice-loopback`, same day, load average 4.9:

| Metric | Injected | n | Mean error (95% CI) | Worst |
| --- | --- | --- | --- | --- |
| response latency | 300 ms | 6 | +3.0 ms [+1.7, +4.8] | 7 ms |
| response latency | 600 ms | 6 | +4.8 ms [+3.3, +6.7] | 9 ms |
| response latency | 900 ms | 6 | +2.5 ms [+1.7, +3.3] | 4 ms |
| barge-in reaction | 150 ms | 6 | +2.0 ms [+0.8, +3.3] | 5 ms |
| barge-in reaction | 300 ms | 6 | +0.8 ms [−0.8, +1.8] | 3 ms |

All within the ±20 ms of SC4.

## Notes found while recording

- **Model download race (#57).** The first `voice_call.py` run crashed the agent while it
  downloaded Kokoro (two warm-up syntheses shared one temporary file); the second run, with the
  models already present, was clean.
- **Cost estimate below actual.** The judged run's estimate said "at most $0.01" and it spent
  $0.0183, well inside its $0.08 cap (#76).
- **Total spend for the demo assets: $0.0183.**
