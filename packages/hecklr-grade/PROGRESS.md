# hecklr-grade progress

Owner: WS4. Spec: `docs/SPEC.md` → Graders, Voice simulation engine › Metrics, Fault-injection
matrix. Contracts: `hecklr_schemas.grading` (`Grader`, `GradeContext`), ADR 0006, ADR 0004.

## Plan

1. Shared helpers: grade construction (`ok` / `skipped` / `error`), event timeline queries.
2. Deterministic graders: `contains`, `not_contains`, `regex`, `max_turns`, `latency_p95_below`.
3. Tool graders: `tool_called`, `tool_not_called`, `args_match`, `call_order`.
4. State: sandboxed simpleeval evaluator with read-only JSON views and ISO date-time strings
   exposing `.weekday` / `.hour`; hypothesis property tests.
5. Voice metrics from events (ADR 0006 definitions on `VoiceMetric`), F7/F8/F9/F12 signatures.
6. `email_threading`, safety graders, custom entry-point graders.
7. LLM judges: 6 versioned rubric files (+ internal `injection_resisted`), content-hash versions,
   structured verdicts, evidence validation, 3-judge majority.
8. `build_graders`, `grade_trial` (deterministic → judges → voice), `trial_passed`.

## Done

- Empty package scaffold (Wave 0): src layout, `py.typed`, smoke test.

## Next

- Steps 1–8 above.

## Open questions

- None yet.
