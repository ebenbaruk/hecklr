# 00hja. The judge calibration set and `hecklr calibrate --judges --set`

Date: 2026-10-23 · Status: proposed · Workstream: WShjudge (Wave 5, SC7)

## Context

SC7 asks for Cohen's κ ≥ 0.70 between each built-in judge and human labels "on a labeled
calibration set". Until now `hecklr calibrate --judges` could only score judge grades stored in
runs, labeled in the UI (ADR 0007c, 0016a). A calibration set needs fixed, realistic
conversations covering the eight rubrics (the six `judge` rubrics plus `injection_resisted` and the
safety pack's `no_policy_override`), labels that survive re-runs, and a way to judge them with any
judge model. The task allowed either an offline script that turns labeled transcripts into a
stored run, or letting `calibrate` judge a labeled transcript file directly.

## Decision

- **Judge the set directly; no stored run.** `hecklr calibrate --judges --set DIR` loads the set,
  turns each case into a `GradeContext`, runs `JudgeGrader` for every labeled (case, rubric) pair
  with the project's `models.judge` through the normal model stack (`open_llm`: rate limits and
  the response cache), and compares with the labels using `hecklr_stats.calibrate_judge`, exactly
  as for stored runs. A stored-run converter would have needed a fake run, fake trials and judge
  calls anyway, and two commands instead of one.
- **Cases are conversations whose tool calls are executed.** `hecklr.judge_calibration` replays
  each `tool` turn against the set's world with `hecklr_world`'s `Toolbox` and `Store` (the code
  the sandbox serves). Results and the "after" snapshot are therefore what a real trial records,
  and an authoring mistake (a call the world refuses without `expect_error`) fails loudly.
  `fault: timeout | http_500` reproduces the sandbox's error texts. E-mail turns become threaded
  `EmailPayload`s, voice turns can be `interrupted`, handoffs are `HandoffPayload` events.
- **Ids are derived from the case id**, so prompts, and with them the response cache, are stable:
  re-running the calibration with an unchanged judge costs nothing.
- **Labels are `HumanLabel` rows in `labels.jsonl`**, the frozen schema the UI already writes.
  `grader` is `judge:<rubric>` for all eight rubrics, including the two whose built-in graders
  report under another name; `grader_version` is the rubric's version hash, so labels stop
  counting when the rubric changes; `grade_id` is derived from the case and rubric because no
  grade exists at labeling time (calibration joins on trial, grader and version, never grade id).
  Verdicts are not in the case files, so review is blind.
- **AI drafts are labeled as such.** The shipped labels are `labeler: "claude-draft"`; people are
  `human:<name>` (written by `calibration/judges/review.py`). `--labeler PREFIX` selects
  labelers, and the command warns whenever it used non-human labels.
- **A rubric with no usable label of its current version fails the self-test**, instead of being
  silently left out of the table.
- **Fix found on the way:** `calibrate --judges --labels` crashed with a `ValueError` traceback
  when a labeled rubric version had grades in the chosen runs but none on labeled trials; such
  groups are now skipped and the usual "no label matches" error is reported.

## Consequences

- SC7 can be measured with one command and one live test (`tests/calibration/
  test_live_sc7_judges.py`, about $0.33 per run on `openai/gpt-6-luna`), but it is **not proven**
  until people label the set and that test passes; the drafts prove only the pipeline.
- Editing any rubric, the judge system prompt or the transcript format invalidates its labels;
  `test_sc7_labels_pin_the_current_rubric_versions` makes that visible in CI, and the rubric must
  be re-reviewed. This is intended: agreement must never be computed across rubric changes.
- The set depends on the acme-maintenance world file; changing its records can change tool
  results under the labels. `test_sc7_every_case_replays_against_the_world` catches refused
  calls, not changed facts, so world edits need a look at the affected cases.
- The UI labeling queue still lists only `judge:*` grades, so the safety graders that delegate to
  a judge (`injection_resisted`, `no_policy_override`, `no_pii_leak`) cannot be labeled on stored
  runs; the set covers their rubrics. Tracked as an issue for the UI owner.
