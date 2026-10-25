# 0024-hstats. Adaptive looks, identical runs and judge drift

Date: 2026-10-24 · Status: accepted · Workstream: WShstats (issues #102, #103)

## Context

Three statistics safeguards the spec promises existed in `hecklr-stats` but no command used them.
`--adaptive` ran one extra batch and compared once at the full α, so O'Brien-Fleming boundaries
were dead code and every comparison recorded look 1 (#103). `flaky_scenarios` and `judge_drift`
had no caller outside the package (#102). Each needs a decision the spec leaves open.

## Decision

**Looks.** An adaptive run has two looks: the first pass and first pass plus the extra batch.
`analyze` recovers the first pass from the matrix (planned cells repeat trial indices
`0 .. trials - 1`; adaptive cells continue from `trials`). `compare_sequentially` tests each look
at its Lan-DeMets O'Brien-Fleming nominal α (information fraction = share of candidate trials) and
stops at the first regression or improvement; "no change" never stops early. A batch smaller than
`MIN_LOOK_GAP` of the trials is not a look of its own. The extra batch goes where Δ's variance is
highest, which the boundaries do not model, so the whole procedure, allocation included, is
simulated instead of assumed: `adaptive_verdict_rate`, 400 seeded A/A replicates, both verdict
directions counted. Measured: 60 × 5: 4.5% [2.9%, 7.0%]; 20 × 3: 3.3% [1.9%, 5.5%] (α = 5%);
power for a 10-point drop at 60 × 5: 98% [93%, 99%]. With `min_effect = 0` the bare test rejects
4.9% [4.0%, 5.9%] over 2,000 seeded replicates at 60 × 5, an interval that covers α
(`test_issue103_the_test_alone_across_looks_is_consistent_with_alpha`). The simulation gives each
scenario one cell; `extend_matrix` spreads a scenario's extra trials over its persona × channel
cells, which changes which conversations run, not the pass/fail counts the test sees. Its extra
share is `hecklr_stats.validation.ADAPTIVE_EXTRA_SHARE`, which a test pins equal to the one
`hecklr run --adaptive` spends (`hecklr.compose`).

**Identical runs.** Flakiness is measured only on runs the caller declares identical
(`analyze(..., identical_runs=True)`); it is never inferred. A shared suite, agent and commit is
not enough: the recorded `git_sha` is HEAD and does not see uncommitted edits, so in the everyday
loop (edit the prompt, `hecklr run`, `hecklr compare`) both runs share a commit, and pooling a real
100% → 0% regression would read as a 50% "flaky" scenario and tell the author to dismiss it. The
same holds for fault-matrix and fixer validation runs. `hecklr calibrate --aa` declares its two
runs identical, since they are the same agent by construction or by the `--runs` contract.
`compare`, `gate`, `report` and the PR comment render the flag whenever an analysis carries it.
Inferring it would need a dirty-tree marker on `Run`, a schema change left for an RFC if wanted.

**Judge drift.** Hecklr keeps no history of judge models, so "changing the judge model triggers a
re-score" is split in two. Reports (`compare`, `gate`, `report`, the PR comment) warn when the
baseline and candidate runs recorded different `judge_model`s and name the re-score command.
`hecklr calibrate --judges --set DIR --drift-from OLD` judges the labeled set with both models and
prints κ before, after and the change with its interval, warning on `JudgeDrift.drifted`. The
warning does not fail the command by itself; an untrusted new judge already does.

## Consequences

- Adaptive comparisons record `look` 1 or 2 and the α spent; an early stop at look 1 is possible.
- Projects outside git never get flaky flags from `compare`; `calibrate --aa` still gives them.
- No automatic re-scoring on a config change: a person runs the named command (it costs judge
  calls, which Hecklr never spends without being asked).
