# hecklr-sim progress

Owner: WS2. Spec: `docs/SPEC.md` → Component specifications → Simulator; Core data model → Scenario,
Persona; US-02. Contracts: `hecklr_schemas.trial` (`CustomerSimulator`, `SimulatorFactory`), ADR 0006.
Decisions: `docs/adr/0002a-simulator-decisions-and-consistency-guard.md`.

## Done (WS2 Done criteria, with evidence)

| Criterion | Evidence |
| --- | --- |
| 4 modes tested with FakeLLM | `test_scripted.py` (7 tests, no LLM calls), `test_guided.py` (guided + `test_ws2_replay_mode_runs_as_guided`), `test_adversarial.py` (`test_us09_*`, strategy registry and factory validation), `test_replay.py::test_us02_replay_scenario_runs_as_an_adaptive_simulation` |
| Contradiction guard: catches wrong ID, changed goal; regenerates; counts | `test_guard.py`: `test_ws2_guard_flags_a_wrong_customer_id`, `..._regenerates_a_wrong_id_and_counts_the_contradiction`, `..._llm_check_catches_a_changed_goal`, `..._counts_one_contradiction_per_regenerated_turn`, `..._gives_up_with_a_harness_error_after_bounded_retries`; review round: `test_us09_cross_customer_probe_runs_through_the_guard_without_contradictions`, `test_us09_guard_accepts_the_probe_strategy_decoys_but_not_other_ids`, `test_ws2_malformed_llm_check_*`, `test_ws2_guard_matches_identifiers_as_whole_tokens`, `test_ws2_guard_accepts_a_new_date_in_the_same_format_as_a_date_fact`; review round 2: `test_us09_guard_accepts_every_probe_tactic_detail`, `test_us09_guard_still_flags_details_beyond_the_probe_decoys`, `test_us09_probe_tactics_point_at_listed_decoys` |
| Guard accepts valid refund/invoice turns (issue #29) | `test_guard.py`: `test_ws2_guard_accepts_valid_refund_turns_issue_29`, `test_ws2_guard_still_flags_real_contradictions_in_refund_turns_issue_29`, `test_ws2_guard_does_not_read_digits_inside_a_longer_identifier_as_a_fact`. A fact shape no longer matches inside a dash-joined ID (`20482` in `INV-20482`), and tokens the goal names are not wrong fact values. All acme-maintenance scenarios' facts plus goal IDs: 16 scenarios flagged before, 0 after |
| Replay extraction tested on 20 transcripts | `tests/fixtures/transcripts.jsonl` (20 synthetic acme conversations: chat, voice, email), `test_us02_replay_extraction_on_20_transcripts`, YAML round trip, grounding, never-overwrite |
| Determinism property test | `test_determinism.py::test_ws2_same_seed_gives_the_same_turns_and_requests` (hypothesis, 40 examples: identical turns and request cache keys); golden seed pinned |
| One live test against Ollama | `test_live_ollama.py` (3 tests, `-m live`): guided conversation never gives a wrong ID, reproducible first turn, grounded replay extraction. 3 passed in 25 s on `qwen3:4b-instruct` |

Numbers (after review round 1): 96 unit tests, 100% line and branch coverage of `hecklr_sim`
(548 statements), `mypy --strict` clean, ruff clean. Full repo suite 352 passed, E2E fake 2
passed, live 3 passed on `qwen3:4b-instruct`.

Exercised for real (scratch script, local Ollama `qwen3:4b-instruct`, cached provider):

- Guided customer vs. a helpful scripted agent → reached `goal_achieved` in 4 turns, gave
  `C-1042` when asked, 0 contradictions.
- Same persona vs. an agent that only says "Sorry, I didn't understand" → `goal_abandoned` after
  its patience tolerance (2), with 1 regenerated turn.
- `refund_social_engineering` persona vs. an agent that insists on verification → claims
  supervisor approval, urgency and public complaint threats; ends with `goal_abandoned`.
- `transcripts_to_scenarios` on the 20 fixtures → 20 valid replay YAML files, all titles correct.

The first live run found 4 prompt and guard defects. All are fixed, with regression tests, in
commit `fix(sim): tighten prompts and guard after live runs against Ollama`.

## Public API (for WS7 / WS14)

- `make_simulator(model, *, max_regenerations=2, llm_consistency_check=False, temperature=None,
  max_tokens=512, strategies=BUILTIN_STRATEGIES, clock_factory=MonotonicClock) -> SimulatorFactory`
- `transcripts_to_scenarios(path, out_dir, llm, *, model, world, persona, seed=0, concurrency=8)
  -> ReplayBatch(written, failures)`; one failed extraction (`LLMOutputError` or
  `ReplayExtractionError`) does not lose the batch, while any other error propagates. Also
  `extract_scenario`, `load_conversations`, `scenario_to_yaml`.
- `AttackStrategy` (with optional `decoys`), `BUILTIN_STRATEGIES` (pass
  `{**BUILTIN_STRATEGIES, **pack}` to add a pack). Every tactic that names someone else's
  details (the shipped probe included) must take them from `decoys`, or the guard rejects
  faithful attack turns as invented; WS14 packs follow the same rule.
- `SimulatorError`: a harness error, raised when no draft survives the guard.
- `hecklr_sim.testing.decision_reply` / `verdict_reply`: FakeLLM replies for the E2E fake pipeline.

## Decisions (see ADR 0002a)

- `max_turns` counts both parties' messages, like `DriverResult.turns`.
- When its script runs out, scripted mode ends with `goal_achieved`. Graders decide success.
- An adversarial persona follows its strategy in any non-scripted mode.
- `contradictions` counts turns the guard regenerated. Invalid JSON is retried but not counted.
- Replay facts are kept only if the customer wrote the value as a whole token (>= 2 chars). Tags
  are capped at 3 plus `replay`.
- Attack decoys live on the strategy, so the guard accepts the probe target's ID (review round 1)
  and any whole token inside a decoy, e.g. the postcode in its address (review round 2).
- Whole-token matching has one definition (`hecklr_sim.tokens`): letters and digits of any
  script are token characters, underscore and punctuation separate tokens.
- Date/time facts get no value shape: a new slot in the same format is not a contradiction.

## Next

- WS7: wire `make_simulator(config.models.simulator)` and `hecklr replay FILE`.
- WS14: add the remaining attack strategies as a pack.

## Open questions

- Voice: fillers, hesitations and barge-in are rendered by `hecklr-voice` (WS9) from
  `Persona.behaviors`. The simulator only adds textual filler guidance. Revisit if WS9 needs
  planned pauses in the text.
