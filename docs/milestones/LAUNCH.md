# Launch: checklist and drafts

Status, 2026-10-23 (WS18). The repository is **private** and nothing below has been published,
posted or deployed. Launch is a decision for the owner: this file prepares it.

Spec: "Launch checklist (M2)", "The 90-second demo", SC1 and SC10. Evidence for the numbers in the
drafts: [`demo/RESULTS.md`](../../demo/RESULTS.md), [M2](M2.md), [M1](M1.md).

## Checklist

| # | Item (spec) | Status | Evidence / what is left |
| --- | --- | --- | --- |
| 1 | README with the demo GIF in the first screen | **done** | `README.md`; `docs/assets/demo.gif` recorded from a real run (`demo/record_gif.sh`) |
| 2 | 3-command quickstart | **done** | README and `docs/quickstart.md` show the same block; SC1 runs it literally in CI (`tests/docs/test_sc1_quickstart.py`, about 10 s locally, limit 300 s) |
| 3 | Comparison table vs other eval tools | **done** | README summary and `docs/comparison.md` (promptfoo, DeepEval, Coval, Cekura), every claim sourced from their docs on 2026-10-23 |
| 4 | Docs site with quickstart, concepts, adapters, statistics explainer | **built, not live** | `mkdocs build --strict` passes locally and in CI (`.github/workflows/docs.yml`). Going live needs: the public repo, a host (GitHub Pages) and the `hecklr.dev` domain (spec open question, unchecked) |
| 5 | Blog post "Your voice agent is slower than you think" | **drafted** | Below. Needs the owner's voice and a place to publish |
| 6 | Show HN, X thread, LinkedIn post with the video | **drafted** | Below. The video itself is not recorded (needs a human): shot list and script in `demo/VIDEO.md` |
| 7 | Posts in the Pipecat and LiveKit communities, with a working example each | **examples ready, posts not drafted** | Pipecat: `pipecat` (SmallWebRTC) and `websocket` `protocol: pipecat` adapters, contract-tested against a real Pipecat pipeline. LiveKit: `livekit` adapter, contract-tested against `livekit-server` in Docker. A standalone 20-line example agent per community is still to write |
| 8 | ≥ 10 issues labeled `good first issue` | **done** | 11 issues (#34, #35, #37, #38, #41, #43, #54, #56, #57, #63, #67), each with a "where / what / test first / check" comment |
| 9 | CONTRIBUTING guide | **done** | `CONTRIBUTING.md`, plus `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), `SECURITY.md`, issue forms |
| 10 | Resume line ready | **drafted** | Below |
| 11 | 90-second demo video (SC10) | **assets ready, video needs a human** | `demo/VIDEO.md`; real runs in `demo/RESULTS.md`; screenshots in `docs/assets/` |
| 12 | Benchmark with ≥ 6 stacks (SC10) | **built, rows preliminary** | `hecklr bench` (PR #80): 8 stacks defined, 7 measured, `reproduce` passes on `loopback-reference`. The rows were measured on a shared laptop (load average up to about 250), with local qwen3 4B customers and judges, and without `acme-chat-luna` (OpenAI credit exhausted): labelled not for publication. Re-measure on an idle machine before launch |
| 13 | `hecklr fix` beat of the demo | **command built, beat not recorded** | `hecklr fix` merged (PR #79). Free end-to-end proof: `packages/hecklr/tests/test_cli_fix.py` fixes F1 on the reference agent and rejects a planted patch that breaks refunds. The demo beat needs a real patch-writing model (OpenAI credit exhausted); the demo stops at the report until then |

Before going public, also:

- [ ] Close the M2 gate's open items (SC3 in CI, SC4 over WebRTC, F10 nightly; see [M2](M2.md)).
- [ ] Reserve the GitHub org `hecklr`, the domain `hecklr.dev`, and the PyPI name (spec, "Name"); publish
      the first release so `pip install hecklr` works (the quickstart and the Action assume it).
- [ ] Enable GitHub private vulnerability reporting (SECURITY.md points to it) and add a
      maintainer contact to CODE_OF_CONDUCT.md if a dedicated address is wanted.
- [ ] Re-check the comparison table against the other tools' docs on launch day.
- [ ] Re-run `demo/chat_gate.sh` and `demo/voice_call.py` on the release commit; update
      RESULTS.md and these drafts if anything moved.

## Resume line

> Creator of Hecklr, an open-source crash-test lab for conversational agents (voice, chat, email;
> statistical CI gating). *Update with stars and users 30 days after launch.*

---

## Draft: blog post

**Your voice agent is slower than you think: measuring turn-taking from the caller's side**

Ask a voice-agent team how fast their agent answers and you will usually get a number from the
agent's own logs. Ask the caller and you get a different one.

Here is a real example. Our reference voice agent (an open-source support agent for a fictional
maintenance company: Silero VAD, faster-whisper, a rules-based brain, Kokoro text-to-speech) logs
how long it takes to answer once it decides the caller has finished. Across one test session of 37
answered turns, its log says **7 ms at the median**. In the same session, measured at the caller's
end of the line, the caller waited **865 ms** at the median (95% CI 847-902 ms), and **1.46 s** at
the 95th percentile.

Nobody is lying. The two clocks start at different moments. The agent's clock starts when its
endpointing decides the turn is over, which by design is 800 ms of silence after the caller's
last word. The caller's clock starts at that last word. Add audio buffering on the way out and the
gap is most of a second, and it never appears in the agent's dashboards.

That is why Hecklr, the open-source testing tool we built, measures turn-taking from the caller's
side, in audio frames:

- **Response latency:** from the caller's last speech frame to the agent's first audio frame.
- **Barge-in reaction:** from the moment the caller starts talking over the agent to the moment the
  agent's audio stops.
- **False interruptions:** the agent starting to talk inside a caller's mid-sentence pause.
- **Dead air:** agent silences over 2 seconds.

**Calibrate the ruler first.** A latency number is only as good as the clock behind it, so Hecklr
checks itself against a loopback agent whose timing is known exactly: it answers a fixed delay
after the caller stops, and stops talking a fixed delay after a barge-in. Over a real WebSocket to
a separate process, across 840 measurements at injected delays of 300, 600 and 900 ms, the worst
error was **13 ms**; the bias was +3 to +4 ms. That check (±20 ms) runs in CI on every change.

**Then break things on purpose.** We seeded one-line faults into the reference agent's voice
settings and checked that the caller-side metrics catch each one, with the clean agent compared
against itself as the control:

| Change | Caller-side metric | Clean → faulty |
| --- | --- | --- |
| 700 ms added before speaking | response latency p50 | 710 ms → 1,417 ms |
| Barge-in disabled | barge-in reaction p50 | 91 ms → 9,059 ms |
| End-of-speech threshold 800 → 150 ms | false interruptions per 100 turns | 0 → 15.6 |
| A weaker speech recognizer | task success at 10 dB SNR | 84.6% → 30.8% |

Each is a statistically significant regression, with its confidence interval, not an eyeballed
difference.

**Noise is not a corner case.** The same clean agent succeeds 92% of the time on a clean line,
85% at 10 dB SNR (café noise), 62% at 5 dB, and 8% at 0 dB. If your tests only use clean audio,
your test results describe a call nobody makes.

**Interruptions are where agents feel rude.** On a noisy street line with a G.711 codec, our
clean agent stopped talking a median of about 150-170 ms after the caller cut in (p95 212 ms).
Turn barge-in off and the caller talks over the agent for 2.7 seconds at the 95th percentile, and
eight out of eight scenarios that test it fail.

**What we learned building this:**

1. Measure at the edge your customer is on. Anything else is an internal metric.
2. Calibrate the instrument against known timing, continuously, or a refactor will move your
   numbers without anyone noticing.
3. Report intervals. Eight calls is not a benchmark, and a tool that prints "212 ms" without
   "[190, 218]" invites over-reading.
4. Keep audio processing off the event loop; with 50 concurrent calls, one blocked loop shows up
   as phantom latency in every call.

Hecklr is open source (Apache-2.0). It runs locally, speaks WebSocket PCM, Twilio Media Streams,
Pipecat and LiveKit, tests chat and email too, and gates pull requests only on statistically
significant regressions. *Link, quickstart and the 90-second video here.*

---

## Draft: Show HN

**Title:** Show HN: Hecklr – crash-test your voice and chat agents with simulated customers

**Text:**

Hecklr is an open-source tool that throws simulated customers at a conversational agent over
voice, chat and email, grades every conversation, and tells you with statistical confidence
whether a change made it better or worse.

I built it because I kept seeing two problems with agent evals. First, text-only evals miss what
breaks voice agents: noisy lines, interruptions, slow turn-taking. Hecklr places real audio calls
(WebSocket PCM, Twilio Media Streams, Pipecat, LiveKit) with background noise, phone codecs and
callers who barge in, and measures latency at the caller's end, calibrated to ±20 ms against a
loopback agent in CI. Second, a pass rate without an error bar is noise. Hecklr pairs runs by
scenario, reports intervals (Wilson, cluster bootstrap), runs a sign-flip permutation test with
Benjamini-Hochberg across slices, and fails a PR only on a significant regression bigger than a
minimum effect you choose. The A/A false-alarm rate is a test in CI.

In the demo GIF, one line of a support agent's prompt changes ("book the first free slot") and the
gate reports −27.3 points [−42.4, −12.1], p = 0.004, with the 9 scenarios that flipped grouped by
cause. Agents' tools run against a per-trial sandbox (MCP and HTTP), so "the visit moved to the
slot the customer asked for" is checked against state, not inferred from text.

It also mines production logs into regression scenarios, ships a small red-team pack, and has a
local UI. Python, Apache-2.0, runs on a laptop; `pip install hecklr && hecklr init && hecklr run`.

Happy to answer questions about the statistics or the audio timing.

---

## Draft: X thread

1/ Your voice agent's logs say it answers in 7 ms. The caller waits 865 ms. Same session, same
agent. We built an open-source tool to measure the second number: Hecklr. (thread)

2/ Hecklr throws simulated customers at your agent over voice, chat and email: real audio calls
with street noise, phone codecs and callers who interrupt. It grades every conversation and gates
your CI.

3/ Latency is measured in audio frames at the caller's end, and the ruler is calibrated: against a
loopback agent with known timing, worst error 13 ms over 840 measurements. That check runs in CI.

4/ Change one line of a prompt ("book the first free slot") and the PR check says: −27.3 points
[−42.4, −12.1], p = 0.004, 9 scenarios flipped, grouped by cause. A regression, with an interval,
not a vibe.

5/ Seeded faults, each caught: +700 ms before speaking (p50 710 → 1,417 ms), barge-in off (91 ms
→ 9 s), a 150 ms end-of-speech threshold (0 → 15.6 false interruptions per 100 turns).

6/ Apache-2.0, local-first, Python. `pip install hecklr && hecklr init && hecklr run`. Video and
docs: *link*.

---

## Draft: LinkedIn

Your voice agent is probably slower than its dashboards say.

In our reference voice agent, the agent's own log reports a median of 7 ms to answer once it
decides the caller is done. Measured at the caller's end of the line, in the same session, the
caller waits 865 ms. The difference is endpointing and audio buffering: invisible from inside the
agent, obvious to the person on the phone.

That gap is why I built Hecklr, an open-source crash-test lab for conversational AI agents. It
calls your agent with simulated customers over voice, chat and email (noisy lines, interruptions,
manipulative callers), grades every conversation against a sandboxed backend, and gates pull
requests only when a regression is statistically significant.

A one-line prompt change in the demo: pass rate −27.3 points [−42.4, −12.1], p = 0.004, nine
scenarios flipped, and one click to the judge's explanation of each failure.

Open source under Apache-2.0. The 90-second video is below; feedback from people running voice or
chat agents in production is very welcome.

*Video, repository link.*
