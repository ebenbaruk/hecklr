# 0002. Typed event payloads as a discriminated union

Date: 2026-10-21 · Status: accepted · Workstream: WS0

## Context

The spec's abridged `Event` has `kind` plus `payload: dict`, noting the payload is "typed per kind
via discriminated union in the real code". Events are read by graders, voice metrics, the miner,
the fixer, every report format and the UI, all written by different builders. An untyped dict would
let each consumer invent its own keys, and errors would surface only at runtime, in someone else's
package.

## Decision

- `Event.payload` is a Pydantic discriminated union on `kind` with one frozen model per event kind:
  `UtterancePayload`, `AudioSpanPayload`, `ToolCallPayload`, `ToolResultPayload`,
  `InterruptPayload`, `SilencePayload`, `EmailPayload`, `HandoffPayload`, `ErrorPayload`.
- `Event.kind` is a read-only property derived from the payload, so the kind cannot disagree with the
  payload and is not stored twice.
- `Event` also carries `id` (ULID) and `seq` (position in the trial log) so evidence can point at a
  single event and events sharing a millisecond keep their order.
- Times are integer milliseconds on the harness monotonic clock (`t_ms`, `start_ms`, `end_ms`);
  `AudioSpanPayload` adds an optional sample index for sample-accurate waveform markers.
- Success criteria use the same pattern, discriminated on `type`, so YAML typos fail at load time.
- Every model is frozen with `extra="forbid"` (artifacts are append-only; unknown keys are errors).
- Hot-path audio frames (`TimedFrame`) are a slotted dataclass, not a Pydantic model, to avoid
  validation cost at 50 calls × 50 frames per second.

## Consequences

- Consumers get static types and exhaustive `match` on payload classes; mypy catches drift.
- Adding an event kind is a schema change and therefore an RFC — intended, since every consumer
  must handle it.
- Storage (DuckDB/Parquet, WS1) flattens payloads by kind or stores them as JSON; the round-trip
  through JSON is property-tested for every payload type.
