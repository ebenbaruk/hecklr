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

- WShstats: the agreement table names its labelers and says "(not human)" for AI drafts (#97,
  `test_cli_calibrate.py`); `--drift-from MODEL` re-judges the set with an earlier judge model and
  warns when κ drops (#102, `test_issue102_changing_the_judge_model_warns_when_agreement_drops`).

- `packages/hecklr/src/hecklr/judge_calibration.py`: set loading, case → trial with executed tool
  calls, labels, `judge_set`. 27 unit tests in `packages/hecklr/tests/test_judge_calibration.py`
  (99-100% line coverage of the module).
- `hecklr calibrate --judges --set DIR [--labels FILE] [--labeler PREFIX]`; uncalibrated rubrics
  fail the self-test; non-human labels are flagged. Fixed a `ValueError` traceback on labels of
  other runs (`test_sc7_judges_ignore_labels_of_other_runs_instead_of_crashing`). 12 new CLI
  tests in `packages/hecklr/tests/test_cli_calibrate.py`.
- The set: 193 conversations (118 chat, 48 voice, 27 e-mail), 434 labeled pairs; per rubric
  42-77 pairs, 47-59% pass (policy_compliance 36/77 = 46.8% to tone 27/46 = 58.7%), 5-25 borderline (table in GUIDE.md). All labels `claude-draft`.
- `tests/calibration/`: 16 set tests, 6 pipeline/CLI tests (κ = 1.00 [1.00, 1.00] for the perfect
  judge on all 8 rubrics; coin flip |κ| < 0.35 each, mean |κ| < 0.15, all untrusted; borderline
  flips 0.2 < κ < 0.95), 6 review-script tests, the cost estimate (434 calls, ~2.73M input tokens,
  ~$0.33 on gpt-6-luna) and the live SC7 test (skipped: no key, no human labels).
- Real CLI run on a scratch project with `fake/judge`: all pairs judged in about 7 s, table printed,
  exit 1 (scripted always-pass judge, κ 0), draft warning shown.
- Review round 1 (PR #89):
  - Label invariant now matches the append-only file: every labeled pair has a label of its
    rubric's *current* version (draft or human), at most one draft per (pair, version); old-version
    rows stay as history. A rubric change fails
    `test_sc7_every_labeled_pair_has_a_label_of_its_current_rubric_version` naming the rubric and
    the `review.py --rubric X` command; the recovery is proven by
    `test_sc7_after_a_rubric_change_appending_a_review_restores_the_invariants`. Size, balance and
    the pipeline tests use the label that counts now (newest current-version human label, else the
    draft).
  - `calibrate --judges --set` judges only pairs with a usable label (`labeled_pairs`) and stops
    with exit 78 before any judge call when there is none (real run:
    `hecklr calibrate --judges --set calibration/judges -c examples/acme-maintenance/hecklr.yaml
    --labeler human:` -> "no label by human: is of a current rubric version ... nothing to
    calibrate", exit 78). `--set`/`--labeler` without `--judges` is exit 78. No more
    `except ValueError: continue` in `_calibrations`.
  - Live SC7 test skips until >= 40 human-labeled pairs per rubric (current version) and judges
    only those pairs. Blind review hides the borderline marker until after the answer.
  - `judge_calibration.py` + `commands/calibrate.py`: 444 statements, 100% line and branch
    coverage; `packages/hecklr` 393 passed, 97% total; `tests/calibration` 31 passed.
- Local real model (free, Ollama `qwen3:4b-instruct`, 2026-10-23) on the two no-world rubrics,
  against the drafts: tone κ 0.57 [0.32, 0.80] (n 46), injection_resisted κ 0.66 [0.43, 0.86]
  (n 42); 88/88 answers usable, 854 s. Not SC7 evidence (small model, AI labels), but the live
  path works end to end and the set discriminates.
- Integration with #86 (2026-10-23): #86 (#54) rewrote the `handoff_appropriate` rubric (an
  emergency ticket counts as an escalation tool; on e-mail the tool call plus the reply is the
  handoff; safety advice alone is not), so its version moved 9751e3507eb7 -> efd2322f000f and the
  45 drafts stopped counting. They were deliberately re-drafted at the new version (45 appended
  `claude-draft` rows, old rows kept as history). No verdict changed: every case with an emergency
  ticket also has a `handoff` event, and `emer-ticket-no-next-steps` still fails because the
  customer is never told what happens next (its note now says so). `tests/calibration` 31 passed.

- **Live run on `openai/gpt-6-luna` against the drafts (2026-10-23, `main` 5c75dd4).**
  `hecklr calibrate --judges --set calibration/judges -c examples/acme-maintenance/hecklr.yaml
  --labeler claude-draft`: 434 pairs judged, exit 0, every rubric κ ≥ 0.70. **This is agreement
  with AI-drafted labels, not with people: it is not SC7 evidence.** SC7 still needs a human
  review of the labels (`review.py`) and the same run with `--labeler human:`.

  | Rubric | n | κ [95% CI] | Precision (fail) | Recall (fail) |
  | --- | --: | --- | --- | --- |
  | `handoff_appropriate` | 45 | 0.82 [0.62, 0.96] | 89.5% [68.6, 97.1] | 89.5% [68.6, 97.1] |
  | `injection_resisted` | 42 | 1.00 [1.00, 1.00] | 100.0% [83.9, 100.0] | 100.0% [83.9, 100.0] |
  | `no_hallucination` | 53 | 0.92 [0.81, 1.00] | 91.7% [74.2, 97.7] | 100.0% [85.1, 100.0] |
  | `no_policy_override` | 46 | 0.87 [0.70, 1.00] | 88.0% [70.0, 95.8] | 100.0% [85.1, 100.0] |
  | `pii_leak` | 52 | 0.92 [0.80, 1.00] | 92.0% [75.0, 97.8] | 100.0% [85.7, 100.0] |
  | `policy_compliance` | 76 | 0.76 [0.61, 0.89] | 83.3% [70.4, 91.3] | 97.6% [87.4, 99.6] |
  | `resolved` | 73 | 0.70 [0.54, 0.86] | 75.0% [59.8, 85.8] | 96.8% [83.8, 99.4] |
  | `tone` | 46 | 0.87 [0.70, 1.00] | 86.4% [66.7, 95.3] | 100.0% [83.2, 100.0] |

  One `policy_compliance` answer was unusable and left out (n 76 of 77). Judge spend $0.4185 for
  the full run plus $0.0065 for a `tone`-only pilot that the full run then read from the cache
  (total $0.4250, above the $0.33 estimate). Where the judge disagrees it is mostly stricter than the
  drafts (recall ≥ 89.5% everywhere, precision 75-100%): on `resolved` it fails 10 pairs the drafts pass and
  passes 1 the drafts fail, so `resolved` sits exactly at the threshold, with a lower bound of
  0.54. Whether the judge or the drafts are right on those pairs is what the human review decides.

## Next / owed

- **SC7 evidence is owed**: people must review the labels (`review.py`), then
  `pytest -m live tests/calibration` must pass on `openai/gpt-6-luna` (needs credit).
- UI labeling queue lists only `judge:*` grades, so `injection_resisted` / `no_policy_override` /
  `no_pii_leak` grades of runs cannot be labeled in the UI (issue #87).

## Decisions

- ADR 00hja: judge the set directly; executed tool calls; ids from case ids; `HumanLabel` rows;
  blind case files; `claude-draft` vs `human:` labelers.
