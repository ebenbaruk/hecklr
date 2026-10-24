# 0024. v1.0 scope decisions: SC3, SC7 and SC10 as the owner accepted them

Date: 2026-10-24 · Status: accepted (decisions by the repository owner, 2026-10-24) ·
Workstream: WShdocs (Wave 6, closing)

## Context

The v1.0 report of 2026-10-24 ([V1.0](../milestones/V1.0.md) at `b4813a4`) left three success
criteria not met. None of them is a defect in Hecklr. Each one is a measurement, a human task or a
publication step that the spec wrote for a public launch:

- **SC3** asks for 50 simultaneous calls "on an 8-core machine". Every measurement is from the
  10-core build laptop (Apple M1 Pro, 8 performance and 2 efficiency cores):
  `test_sc3_fifty_simultaneous_calls_without_underruns` (50 calls, 0 underruns, worst send
  lateness 7.5 ms) and `examples/voice-loopback/sc3_load.py --calls 50` over the network
  (0 underruns, worst lateness 18.8 ms, latency error p95 4.0 ms [3, 4], **0.78 cores used in
  total**).
- **SC7** asks for Cohen's κ ≥ 0.70 "vs human labels". The calibration set
  (`calibration/judges`, 434 labeled pairs over chat, voice and email, ADR 00hja) was judged
  live on `openai/gpt-6-luna` (PR #96): every rubric reached κ ≥ 0.70, from 0.70 [0.54, 0.86]
  (`resolved`) to 1.00 [1.00, 1.00] (`injection_resisted`). Every label is `claude-draft`: an
  AI drafted it, no person reviewed it.
- **SC10** asks for a docs site, a 90-second demo video and a benchmark with ≥ 6 stacks. The site
  builds with `mkdocs build --strict` and is not hosted (the repository is private). The video
  was never recorded. The benchmark measures 8 stacks, on a shared laptop, labelled preliminary.

The owner reviewed the list on 2026-10-24 and decided what v1.0 needs. This ADR records those
decisions so the v1.0 report can rely on them and nobody has to rediscover them.

## Decision

1. **SC3 is accepted on the 10-core measurement.** At 50 calls the whole system used 0.78 cores,
   less than one tenth of an 8-core machine, so an 8-core run would test headroom that the
   measurement already shows. No separate 8-core run is owed. The per-merge `perf` step keeps
   the test running. Under heavy host load it can fail (#121); that is a property of a shared
   gate machine, handled by ADR 0022a's rerun rule.
2. **SC7 is accepted for v1.0 on agreement with AI-drafted labels**, and is always described that
   way: "κ 0.70-1.00 per rubric against AI-drafted (`claude-draft`) labels, not human labels".
   Hecklr's own docs and reports must not call it human agreement (#97). Teams calibrate their
   own judges on their own labels with `hecklr calibrate --judges` (labels from the UI, or a set
   with `--set`), as the [judge calibration guide](../guides/judge-calibration.md) describes. A
   human review of the shipped labels (`calibration/judges/review.py`, then the live run with
   `--labeler human:`) is a documented optional step, not a release blocker.
3. **SC10:**
   - **No demo video.** The owner dropped it. `demo/VIDEO.md` is no longer a blocker; the demo
     scripts (`demo/chat_gate.sh`, `demo/voice_call.py`) stay, because they reproduce the GIF and
     the numbers in the docs.
   - **The docs site builds strictly in every gate.** Hosting and publication happen when the
     owner makes the repository public. Until then the site is a build artifact.
   - **The benchmark stays labelled preliminary.** Its 8 rows (≥ 6 required) exist and
     `hecklr bench reproduce` passes; re-measuring on an idle machine is a launch task.
4. **GitHub Actions is re-enabled by the lead at the end of this wave.** ADR 0022a's local gate,
   `scripts/ci-local.sh`, stays documented as the fallback when Actions is unavailable.
5. **Launch-only items move to the launch checklist**, not the v1.0 gate: publishing to PyPI,
   hosting the docs, the leaderboard, the launch posts and the ≥ 10 open `good first issue`
   issues (#111). They need a public repository, and making it public is the owner's decision.

## Consequences

- The v1.0 gate can pass with SC3, SC7 and SC10 marked "met under ADR 0024". The report states
  exactly what was accepted, so a reader can still see the difference from the spec's wording.
- SC7's number is weaker evidence than the spec intended. The risk is that the AI drafts and the
  judge share a blind spot. The mitigation is in the product: `hecklr calibrate --judges` refuses
  to trust a rubric with fewer than 10 human labels of each class (ADR 00rtc). The README and the
  guides tell teams to label their own trials before they trust a judge.
- SC3 on a smaller machine is untested. Someone running 50 calls on fewer cores can measure their
  own machine with `examples/voice-loopback/sc3_load.py --calls 50` (underruns, loop lag, CPU).
- The spec is not edited. Its success criteria stay as the original target, and this ADR is the
  record of how v1.0 met them.
