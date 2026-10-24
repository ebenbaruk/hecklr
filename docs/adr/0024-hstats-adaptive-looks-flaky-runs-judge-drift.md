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
5.4% [4.5%, 6.5%] over 2,000 replicates (5.6% with the final look alone, 4.0% fixed): the looks
cost nothing, the variance-targeted batch about 1.5 points, which the minimum effect absorbs.

**Identical runs.** Flakiness is measured only on runs of the same suite, agent (name and version)
and recorded commit (`is_identical`). Runs without a commit are not identical: pooling a real
100% → 0% change would read as a 50% "flaky" scenario. `hecklr calibrate --aa` declares its two
runs identical (`analyze(..., identical_runs=True)`), since they are the same agent by
construction or by the `--runs` contract.

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
