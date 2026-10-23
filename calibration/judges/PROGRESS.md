# Judge calibration set (WShjudge, SC7): progress

## Plan

1. A way to judge labeled transcripts directly (`hecklr calibrate --judges --set DIR`), ADR 00hja.
2. The set: ≥ 40 balanced, realistic (case, rubric) pairs per built-in rubric over chat, voice and
   e-mail, with borderline cases; AI-drafted labels marked `claude-draft`, written by GUIDE.md.
3. A review script for people (`review.py`, labels `human:<name>`).
4. Offline tests: set validity and balance; the pipeline with FakeLLM judges (perfect → κ 1,
   coin flip → κ ≈ 0, wrong on borderline → intermediate); the CLI.
5. A live SC7 test (skipped without key, budget and human labels) and its cost estimate.
6. Docs page and nav entry.

## Done (evidence)

- `packages/hecklr/src/hecklr/judge_calibration.py`: set loading, case → trial with executed tool
  calls, labels, `judge_set`. 27 unit tests in `packages/hecklr/tests/test_judge_calibration.py`
  (99-100% line coverage of the module).
- `hecklr calibrate --judges --set DIR [--labels FILE] [--labeler PREFIX]`; uncalibrated rubrics
  fail the self-test; non-human labels are flagged. Fixed a `ValueError` traceback on labels of
  other runs (`test_sc7_judges_ignore_labels_of_other_runs_instead_of_crashing`). 12 new CLI
  tests in `packages/hecklr/tests/test_cli_calibrate.py`.
- The set: 193 conversations (118 chat, 48 voice, 27 e-mail), 434 labeled pairs; per rubric
  42-77 pairs, 52-58% pass, 5-25 borderline (table in GUIDE.md). All labels `claude-draft`.
- `tests/calibration/`: 16 set tests, 6 pipeline/CLI tests (κ = 1.00 [1.00, 1.00] for the perfect
  judge on all 8 rubrics; coin flip |κ| < 0.35 each, mean |κ| < 0.15, all untrusted; borderline
  flips 0.2 < κ < 0.95), 6 review-script tests, the cost estimate (434 calls, ~2.73M input tokens,
  ~$0.33 on gpt-6-luna) and the live SC7 test (skipped: no key, no human labels).
- Real CLI run on a scratch project with `fake/judge`: all pairs judged in about 7 s, table printed,
  exit 1 (scripted always-pass judge, κ 0), draft warning shown; with `--labeler human:` every
  rubric is reported uncalibrated (exit 1).
- Local real model (free, Ollama `qwen3:4b-instruct`, 2026-10-23) on the two no-world rubrics,
  against the drafts: tone κ 0.57 [0.32, 0.80] (n 46), injection_resisted κ 0.66 [0.43, 0.86]
  (n 42); 88/88 answers usable, 854 s. Not SC7 evidence (small model, AI labels), but the live
  path works end to end and the set discriminates.

## Next / owed

- **SC7 evidence is owed**: people must review the labels (`review.py`), then
  `pytest -m live tests/calibration` must pass on `openai/gpt-6-luna` (needs credit).
- UI labeling queue lists only `judge:*` grades, so `injection_resisted` / `no_policy_override` /
  `no_pii_leak` grades of runs cannot be labeled in the UI (issue #87).

## Decisions

- ADR 00hja: judge the set directly; executed tool calls; ids from case ids; `HumanLabel` rows;
  blind case files; `claude-draft` vs `human:` labelers.
