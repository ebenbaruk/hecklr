# 00rtc. A judge is trusted only on enough labels of both classes

Date: 2026-10-24 · Status: accepted · Workstream: red-team P0/P1 fixes (issue #100)

## Context

Cohen's κ is `(observed − chance) / (1 − chance)`. When both raters give one label to every item,
chance agreement is 1 and κ is 0/0; Hecklr defined it as 1. `JudgeCalibration.trusted` used the
point κ alone, with no minimum number of labels. So a rubric calibrated on one label, or on labels
that are all "pass", was reported as κ = 1.00 [1.00, 1.00], trusted, even for a judge that always
answers "pass", and `hecklr calibrate --judges` exited 0 (SC7 passed). The `--labels FILE --run
ID` path hits this directly: a few labels of a mostly passing run. The shipped calibration set is
balanced (at least 19 labels of each class per rubric), so `--set` was not affected.

## Decision

A rubric is trusted only when the humans labeled at least `MIN_LABELS_PER_CLASS` = 10 passes and
10 fails, and κ ≥ 0.70. `JudgeCalibration` carries `human_passes` and `human_fails`, and
`untrusted_reason` says which rule failed ("needs 10+ human labels of each class, has 20 pass and
0 fail", or "kappa < 0.70"); every report, the terminal and `hecklr calibrate` print it. The 0/0
convention is kept (the raters never disagreed), and it no longer decides anything: with both
classes among the human labels, the point κ is always defined.

Ten per class follows the spec's "stratified sample": it is the fewest at which a judge that
misses failures shows it (one missed fail out of 10 already costs about 0.1 of κ), and small
enough for a person to label in one sitting.

## Consequences

- The issue's repros (an always-pass judge on 1, 2, 5 or 20 all-pass labels) are untrusted, and
  the SC7 self-test fails with the reason.
- Small hand-labeled sets that used to pass now fail SC7 until both classes reach 10. The tests
  that relied on 1 to 4 labels per rubric now label 10 of each class.
- The UI's agreement view shows such rubrics as untrusted, as the CLI does.
