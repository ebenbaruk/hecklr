# 0006. Trial-loop Protocols, one event sink, and measurement definitions in the schema

Date: 2026-10-21 · Status: accepted · Workstream: WS0

## Context

Wave 1 builds the orchestrator (WS1), simulator (WS2), world sandbox (WS3) and chat channel (WS6)
in parallel. Each needs the others' interfaces. Graders (WS4), statistics (WS5) and reports (WS7)
read what the channels write, so every metric they compute needs an agreed definition in terms of
event fields. The UI writes judge labels and scenario approvals, and the miner and `hecklr replay`
read real transcripts. If no shared contract exists for these, each builder invents one and the
integrator needs RFCs straight away.

## Decision

- **Trial loop (`hecklr_schemas.trial`).** Four small Protocols and their data:
  - `CustomerSimulator.next_turn(dialogue) -> SimTurn | SimEnd`, plus `contradictions`, built by a
    `SimulatorFactory(brief, llm)`. The simulator sees only `DialogueTurn`s (speaker and text), so
    it cannot read the agent's tool calls or other hidden state.
  - `WorldSandbox.open(brief, world, sink) -> WorldSession` with `mcp_url`, `http_url`,
    `snapshot()` and `close()`. Faults come from `brief.scenario.faults` and are seeded from
    `brief.seed`. Observed mode (`agent.tool_mode: observed`) uses the same session, with no URLs.
  - `ChannelDriver.run(ctx, brief, simulator, sink) -> DriverResult`. The driver raises on harness
    failures, which the orchestrator retries. It reports agent failures and timeouts in the result.
  - `EventSink`: the trial's single append-only log and clock. The world and the driver both write
    to it, so one trial has one ordered log. The sink, not the caller, assigns ids and `seq`.
    `emit(..., t_ms=...)` records an event after the fact, stamped with the time it started.
- **One time origin.** Every `*_ms` instant (events, payload spans, world snapshots, audio frames)
  is milliseconds since the trial started on the harness monotonic clock.
- **Chat and voice timing on `UtterancePayload`.**
  - `Event.t_ms` is the turn's start: the first token or audio frame for the agent; the message
    sent or speech onset for the user.
  - `end_ms` is the turn's end.
  - `latency_ms` (agent turns only) runs from the end of the user turn being answered.
  - Chat TTFT is `latency_ms`. Chat TTLT is `latency_ms + end_ms - t_ms`. Voice response latency is
    `latency_ms`.
  - `audio_span_id` links a transcript to its audio.
- **Voice metrics come from events only.**
  - `SilencePayload.reason` separates ordinary gaps from planned caller hesitations and
    re-engagement silences.
  - `InterruptPayload.reaction_ms` gives barge-in reaction time and shows a backchannel wrongly
    stopping the agent.
  - The derivation of each metric is written on `VoiceMetric`.
- **Estimates.** `Estimate(value, ci_low, ci_high, confidence, n, method)` is the only way to carry
  a statistic. `Comparison` holds baseline, candidate and delta as estimates. `MetricSummary`
  stores per-run aggregates. Group-sequential `look` and `alpha_spent` are recorded.
- **Review records.**
  - `HumanLabel` pins `grader_version`, so κ never mixes rubric versions.
  - `ScenarioProposal` holds a mined scenario and its review state. A decision is appended under
    the same id, and the newest record wins, as with `Run`.
- **Transcripts.** `Conversation` is the native JSONL line (one conversation per line). Other
  miner sources convert into it. It is exported as JSON Schema with the authored files.
- **SNR sweeps.** The degradation curve (US-07) needs trials at several SNR levels. For v1, write
  one persona per bucket (e.g. `camille-car-10db`). Reports slice by `persona.voice.snr_db`. A
  matrix axis would change `TrialSpec.key` and needs a way to represent "clean". It can be added by
  RFC if persona cloning proves too painful.

## Consequences

- Wave 1 builders code against fixed signatures. The contract tests in
  `tests/test_trial_loop.py` run a whole fake trial through the Protocols.
- A driver can log a reply only after it completes, so `seq` order can differ from time order.
  Timelines sort by `(t_ms, seq)`.
- Skipped and error grades can never pass. Statistics read only `ok` grades.
- Unknown-priced paid models are refused, not treated as free (see `ModelsConfig.prices` and
  `hecklr-llm`).
