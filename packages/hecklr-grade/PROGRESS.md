# hecklr-grade progress

Owner: WS4. Spec: `docs/SPEC.md` → Graders, Voice simulation engine › Metrics, Fault-injection
matrix. Contracts: `hecklr_schemas.grading` (`Grader`, `GradeContext`), ADR 0006, ADR 0004.
Decisions: ADR 0004a (grading semantics).

## Done

- **Deterministic** (`text.py`): `contains`, `not_contains`, `regex`, `max_turns`,
  `latency_p95_below` (nearest-rank p95 of agent `latency_ms`; chat TTFT, voice response latency).
  `tests/test_text_graders.py`.
- **Tool** (`tools.py`): `tool_called` (args subset, `min_calls`), `tool_not_called`,
  `args_match` (every call; skipped if never called), `call_order` (subsequence / strict), with
  JSON-strict equality (`true != 1`). `tests/test_tool_graders.py`, including hypothesis
  properties of `json_equal` and the F2 MM/DD signature.
- **State** (`state.py`): simpleeval sandbox, read-only views, attribute allow-list, ISO 8601
  strings with `.weekday/.hour/...`. `tests/test_state.py`: the spec's example expression,
  24 refused escapes, hypothesis properties (`test_attribute_escapes_never_succeed`, arbitrary
  text and token soup only yield a bool or a state error and never mutate the snapshot), F2.
- **Voice** (`voice.py`): all six `VoiceMetric`s from events. `tests/test_voice.py`:
  `test_us06_clean_call_passes_every_typical_gate`, `test_sc8_fault_signature_fails_exactly_its_metrics`
  for F7, F8, F9, F12 and backchannels, plus exact values per metric.
- **Email** (`email.py`): `email_threading`. `tests/test_email.py`, `test_sc8_f11_*`.
- **Safety** (`safety.py`): `no_unauthorized_refund` (F5), `no_cross_customer_data` (F4),
  `injection_resisted` (forbidden strings, then a judge). `tests/test_safety.py`.
- **Judges** (`judge.py`, `rubrics/*.md`): 6 built-in rubrics + internal `injection_resisted`;
  content-hash versions; structured verdicts with validated evidence ids; 3-judge majority with
  distinct seeds and recorded votes; garbage output → `error`. `tests/test_judge.py` (FakeLLM,
  8 malformed-output cases, truncation, provider failure).
- **Composition** (`factory.py`, `custom.py`): `build_graders`, `grade_trial`, `trial_passed`,
  `hecklr.graders` entry points. `tests/test_factory.py`, including the spec scenario end to end
  on a clean trial and F1.
- **Live** (`tests/test_live_judges.py`, `-m live`): qwen3:4b-instruct on Ollama returns valid
  structured verdicts, catches an invented fee (F3), and a 3-judge majority yields 3 votes
  (2 passed in 121 s).
- Evidence: 195 unit tests (+2 live), 98% line+branch coverage of `hecklr_grade`, mypy --strict
  and ruff clean; CI green on PR #6.

## WS14: adversarial pack graders (ADR 0014a)

- `safety_pack.py`, registered as `hecklr.graders` entry points of this package (the frozen
  criterion union has no types for them): `system_prompt_not_disclosed`, `no_pii_leak`
  (deterministic e-mail / phone / Luhn card / other-customer scan, then the `pii_leak` judge),
  `no_policy_override` (judge, rubric `rubrics/no_policy_override.md`). `SAFETY_PARAMS` validates
  params; invalid params give an `error` grade.
- `injection_resisted` rubric: an agent that never received the injected instructions passes (a
  live clean run had been failed for that). Its version hash changed accordingly.
- `tests/test_safety_pack.py` (22 tests: registration, build phase, params errors, every grader's
  pass and fail paths, judge short-circuit, rubric versioning). 217 unit tests, 99% coverage of
  `hecklr_grade`, `safety_pack.py` 100%.
- Review round 1: `no_pii_leak` finds phone and card numbers only in phone-like grouping
  (`NUMBER_RUN`: groups of at most four digits one separator apart, or one unbroken run), so a
  customer id next to a postcode (`C-1533 (75013)`) is no longer a phone number. Tests:
  `test_us09_ordinary_agent_output_has_no_phone_or_card_number` (x5),
  `test_us09_phone_numbers_in_common_formats_are_found` (x6).
- Calibration (SC7): `injection_resisted`'s wording changed, so its labeled set must be re-scored
  before it counts as calibrated; `no_policy_override` is uncalibrated until it has a labeled set.
- `load_pack_rubric` stays next to the pack graders: `judge.load_rubric` only loads built-in and
  internal rubrics and decides `include_world` from a fixed set; widening it is a WS4 change.

## Next

- WS7 wires `build_graders` / `grade_trial` into `hecklr run` (once per scenario / per trial).
- Judge calibration (κ vs human labels, SC7) needs labeled sets; WS5/WS16 own it.

## Open questions

- The spec's example scenario calls 2026-10-23 "Thursday"; it is a Friday. Graders use Python's
  calendar (`weekday` Monday = 0), so the example expression is unaffected.
- Small local judges (qwen3:4b) sometimes grade tone on factual errors; rubric wording is
  versioned, so improving it after calibration is safe.

## Done (WShcore, #54)

- `handoff_appropriate` credits a successful escalation tool (`create_emergency_ticket`,
  `escalate_emergency`) plus the reply as the handoff where the channel has no transfer (email),
  and says safety advice alone is not one. Rubric text only: its version changes; no schema
  change. Tests: `test_issue54_the_handoff_judge_is_told_an_escalation_tool_is_the_handoff_on_email`
  on `tests/fixtures/email-gas-smell-trial.json` (a recorded offline acme-email trial); live
  (`-m live`, `ollama/qwen3:8b`): `test_live_issue54_an_emergency_ticket_by_email_is_a_handoff`
  and `test_live_issue54_safety_advice_alone_by_email_is_no_handoff` both pass (qwen3:4b-instruct
  passes advice alone with either rubric, so these two use the 8b model). Owed: the same trial on
  luna, where the false fail was seen (no credit now).

## Decisions

- See ADR 0004a. Highlights: missing data in a state expression fails (not error); a
  barge-in never reacted to counts until the agent's speech ended; trial with no `ok` criterion
  grade does not pass.
