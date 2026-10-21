# 0002a. Simulator: one structured decision per turn, a deterministic guard, hashed seeds

Date: 2026-10-21 · Status: accepted · Workstream: WS2

## Context

The simulated customer (`hecklr-sim`) implements the frozen `CustomerSimulator` and
`SimulatorFactory` Protocols (ADR 0006). The spec asks for four modes, information discipline,
structured stop reasons, a self-consistency guard whose contradiction rate is reported, replay
extraction from real transcripts, and reproducibility from (config, seed, cached responses). The
Protocol gives the simulator only the dialogue (`DialogueTurn`s) and an `LLMProvider`; it has no
event sink and no clock.

## Decision

- **One structured call per turn.** The private brief (persona, trait guidance as text, goal,
  facts, stop conditions, optional attack strategy) is the system prompt; the user message is the
  visible transcript plus the customer's state. The model answers a `SimDecision`
  (`action`, `text`, `end_reason`, `facts_revealed`, `agent_made_progress`, `subject`) through
  `complete_structured`. All fields are required because small models fill required fields
  reliably.
- **Modes.** `scripted` → `ScriptedSimulator`, which never calls a model; its script position is
  the number of customer messages in the dialogue, so it holds no state. Running out of script
  ends with `goal_achieved`: graders, not the end reason, decide success. `guided` and `replay` →
  `GuidedSimulator`. A persona with `adversarial` set follows its strategy in any non-scripted
  mode, so red-team personas can be crossed with ordinary scenarios; `adversarial` mode without a
  strategy is a configuration error raised when the simulator is built.
- **Strategies are data, with no global registry.** `AttackStrategy` (objective, tactics, success
  signal) is rendered into the brief. `BUILTIN_STRATEGIES` is read-only; packs pass
  `strategies={**BUILTIN_STRATEGIES, **pack}` to `make_simulator`.
- **Patience is deterministic.** The tolerance is `1 + floor(5 · patience)` unproductive agent
  messages in a row, plus one more on a seeded coin flip. The model reports `agent_made_progress`
  using a written rule (asking for needed details counts as progress). The harness counts the
  streak, tells the model when it is about to give up, and ends with `goal_abandoned` if the model
  keeps talking past its tolerance.
- **Limits.** `max_turns` counts both parties, like `DriverResult.turns`. `max_duration_s` uses a
  `Clock` from `make_simulator(clock_factory=...)`, started when the trial's simulator is built.
  The driver still enforces limits in the middle of a turn. The simulator's own check lets the
  customer leave cleanly with a structured reason.
- **The guard is deterministic first.** It flags tokens shaped like a fact value but different
  from it, ID-like tokens found nowhere in the brief or the agent's words, unknown
  `facts_revealed`, and malformed decisions (including JSON spilled into the text). An optional
  LLM audit (`ConsistencyVerdict`) catches a changed goal. A flagged draft is regenerated with
  the issues as feedback, up to `max_regenerations` times. `contradictions` counts turns that
  needed regeneration. When no attempt is left, the guard raises `SimulatorError`, a harness
  error, so a broken simulation is retried or excluded instead of failing the agent. Invalid JSON
  is retried within the same budget but is not counted as a contradiction.
- **Seeds are hashed, never drawn.** Every request seed is
  `blake2b(trial seed, purpose, turn, attempt)` mod 2³¹−1, pinned by a golden test. An unchanged
  turn always produces an identical request and so a cache hit. The value does not depend on how
  many draws came before.
- **Replay grounding.** Extracted facts are kept only if the customer actually wrote the value.
  Success criteria are deliberately minimal: the `resolved` judge, plus `tool_called` on the
  resolving tool only if the transcript called it. Authors refine the rest.
  `transcripts_to_scenarios` never overwrites an existing file.

## Consequences

- WS7 wires `make_simulator(config.models.simulator)` and `transcripts_to_scenarios` directly.
  WS14 adds strategies without touching this package.
- The deterministic guard can raise false positives on legitimate numbers shaped like a fact
  (e.g. a 5-digit amount when the postcode has 5 digits). Regeneration absorbs them at the cost
  of a counted contradiction. The contradiction rate in reports makes such drift visible.
- Progress judgement still depends on the model. Small local models are noisier, which the live
  tests check only as plumbing (ADR 0005).
