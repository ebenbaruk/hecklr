# hecklr-sim progress

Owner: WS2. Spec: `docs/SPEC.md` → Component specifications → Simulator; Core data model → Scenario,
Persona; US-02. Contracts: `hecklr_schemas.trial` (`CustomerSimulator`, `SimulatorFactory`), ADR 0006.

## Plan

1. `seeds`: stable seed derivation (trial seed + labels → 31-bit seed) for every LLM call and draw.
2. `scripted`: fixed `user_turns`, no LLM; position derived from the dialogue, so it is stateless.
3. `guided` (also `replay` and `adversarial`): private brief → structured `SimDecision` via
   `complete_structured`; patience modeled from traits with a seeded jitter; limits → `SimEnd`.
4. `guard`: deterministic checks (wrong fact values by shape, invented identifiers, unknown facts,
   malformed decisions) plus an optional LLM check (changed goal); bounded regeneration; counts.
5. `strategies`: `AttackStrategy` + `BUILTIN_STRATEGIES` (refund social engineering, cross-customer
   data probe); the factory takes a mapping so WS14 can add a pack without global state.
6. `replay`: `Conversation` → replay `Scenario` (LLM extraction, facts grounded in what the customer
   actually said), `load_conversations`, `transcripts_to_scenarios(path, out_dir, ...)` → YAML.
7. `make_simulator(model, ...)` → `SimulatorFactory` for WS7 to wire.

## Done

- Empty package scaffold (Wave 0): src layout, `py.typed`, smoke test.

## Next

- Everything in the plan.

## Open questions

- None yet.
