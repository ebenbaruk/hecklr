# 00rtb. Scenarios whose every trial was excluded block the gate

Date: 2026-10-24 · Status: accepted · Workstream: red-team P0/P1 fixes (issue #99)

## Context

Harness errors and undecided trials (every criterion passed except some whose grade is `error`)
are excluded from the statistics (ADR 0007b). Pairing then keeps only the scenarios both runs
analyzed. When every trial of a scenario is excluded in one run, the scenario leaves the
comparison without notice, and `Analysis.problems` only fired when nothing was shared at all. A
candidate that broke 12 of 40 scenarios in a way that made their judges error (an oversized
reply overflowing the judge's context, injected text that makes it answer garbage) or the
harness fail was gated on the 28 it did not break: INCONCLUSIVE instead of a 23-point REGRESSION,
and NO CHANGE (exit 0) when the rest were deterministic. The fixer's guard pass had the same hole.

## Decision

- `analyze` adds a problem (so the gate exits 3, harness error) for every run in which a scenario
  that both runs ran has no analyzable trial, naming the scenarios. It applies to the baseline
  too, and only when both runs analyzed something (otherwise the existing problems already say
  so). Scenarios that only one run ran (the suite changed) and scenarios that kept at least one
  analyzable trial are compared as before.
- The fixer's guard pass counts a candidate cell without a verdict, whose baseline cell has one,
  as a failed cell. The targeted pass is unchanged.

No threshold: a scenario Hecklr could not measure in one run is reported, not silently dropped.
Exit 3 is the spec's answer for "Hecklr could not measure this"; the problem text says which
scenarios and why, so a rerun or a judge fix follows.

## Consequences

- A judge or harness failure that wipes out a whole scenario in one run now fails the gate as a
  harness error instead of being invisible. With `trials: 1`, one such trial is enough; that is
  the honest outcome, since the scenario has no measurement.
- Partial exclusions still shrink a scenario's sample without a problem; their counts stay in
  every report. The check is per scenario, not per slice cell: a scenario that kept an analyzable
  chat trial but lost every voice trial still drops out of the `channel=voice` slice without a
  problem. The suite verdict, which gates, is covered; slices only report, so this stays a known
  limit rather than a second check.
- In the guard, exclusions in the candidate lean towards rejecting the patch, never towards
  accepting it: the guard exists to keep regressions out.
