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
- Evidence: 197 unit tests, 98% line+branch coverage of `hecklr_grade`, mypy --strict and ruff
  clean.

## Next

- WS7 wires `build_graders` / `grade_trial` into `hecklr run` (once per scenario / per trial).
- Judge calibration (κ vs human labels, SC7) needs labeled sets; WS5/WS16 own it.

## Open questions

- The spec's example scenario calls 2026-10-23 "Thursday"; it is a Friday. Graders use Python's
  calendar (`weekday` Monday = 0), so the example expression is unaffected.
- Small local judges (qwen3:4b) sometimes grade tone on factual errors; rubric wording is
  versioned, so improving it after calibration is safe.

## Decisions

- See ADR 0004a. Highlights: missing data in a state expression fails (not error); a
  barge-in never reacted to counts until the agent's speech ended; trial with no `ok` criterion
  grade does not pass.
