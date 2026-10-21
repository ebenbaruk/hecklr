# hecklr-schemas progress

Owner: WS0 (architect). **Frozen after Wave 0**: changes need an RFC in `docs/rfc/`.
Spec: `docs/SPEC.md` → "Core data model", "Agent adapters", "Graders", "Configuration".

## Done

- `common`: `FrozenModel` (frozen, `extra="forbid"`), `Channel`, `Actor`, `Budget`, id and model
  string types, `SCHEMA_VERSION`.
- `ids`: ULID NewTypes (`RunId`, `TrialId`, `EventId`, `GradeId`, `ComparisonId`, `ClusterId`,
  `PatchId`), validating field types, `new_id()`, `is_ulid()`.
- `clock`: `Clock` Protocol, `MonotonicClock`, `FakeClock`.
- Authoring models: `Scenario` (17 success-criterion types, modes, limits, provenance, tool faults),
  `Persona` (traits, behaviors, voice profile), `World` (collections, declarative tools, ADR 0004),
  `HecklrConfig` (adapters per channel for all 10 adapter types, suites, models, stats, budget,
  baseline, gate). The spec's YAML examples parse verbatim (`tests/fixtures/spec_*.yaml`).
- Runtime models: `TrialSpec`, `Run`, `Trial`, `Event` with typed payloads (ADR 0002), `EventRef`,
  `Grade`, `Comparison`, `FailureCluster`, `Patch`, `WorldSnapshot`.
- Protocols: `ChatTarget`, `ChatSession`, `VoiceTarget`, `AudioLink`, `EmailTarget`, `Grader`,
  `LLMProvider`; data: `SessionContext`, `AgentChunk`, `SessionSummary`, `TimedFrame`,
  `EmailMessage`, `GradeContext`, `LLMRequest`, `LLMResponse`.
- JSON Schema export for authored files (`authoring_json_schemas`, `write_json_schemas`).
- Review round (ADR 0006):
  - Trial-loop Protocols: `EventSink`, `TrialBrief`, `CustomerSimulator` with `SimTurn`/`SimEnd`
    and `SimulatorFactory`, `WorldSandbox` with `WorldSession`, and `ChannelDriver` with
    `DriverResult`.
  - Timing fields: `end_ms`, `latency_ms` and `audio_span_id` on utterances; `reason` on
    silences; `reaction_ms` on interrupts. Each `VoiceMetric` documents how it is derived.
  - `Estimate`/`MetricSummary`; `Comparison` now carries estimates plus `look` and `alpha_spent`.
  - `Run` gains `baseline_id` and `adaptive`.
  - `JudgeVote` votes on `Grade`. Only `ok` grades can pass.
  - New records: `HumanLabel`, `ScenarioProposal`, and `Conversation` (native JSONL).
  - `FailureCluster` validates that members match `source`, and exemplars are split by kind.
  - `ToolFault` rejects parameters that belong to other fault kinds.
  - `AgentConfig.tool_mode` (sandbox or observed) and `ModelsConfig.prices`.
  - `LLMResponse.truncated`.
- Tests: 135, including Hypothesis JSON round trips for every event payload, trials, grades and
  runs; line and branch coverage 100%.

## Next

- Nothing until an RFC is approved.

## Notes for consumers

- YAML loaders must keep ISO timestamps as strings (e.g. a `SafeLoader` without the timestamp
  resolver); `facts` and seed records are JSON values, and PyYAML's default turns `2026-10-23` into
  a `date`.
- Suite loaders must check that `mode: adversarial` scenarios only cross personas that have an
  `adversarial` strategy (a scenario cannot see persona definitions).
- `WorldSnapshot` record keys are strings: integer key `1` is `world.orders["1"]`.
- `datetime` fields are wall-clock metadata and must be timezone-aware; durations and event times
  always come from the harness clock in integer milliseconds.

## Open questions

- None.
