# hecklr-grade

Hecklr graders: deterministic, tool, state, LLM judge, voice and safety.

```python
from hecklr_grade import build_graders, grade_trial, trial_passed

graders = build_graders(scenario)  # once per scenario; raises on bad criteria
grades = await grade_trial(ctx, graders)  # ctx: GradeContext of one finished trial
passed = trial_passed(grades)  # every ok success-criterion grade passed
```

- One grader per success-criterion type; voice metrics and email threading are added as
  diagnostics on voice and email scenarios.
- State expressions run in a simpleeval sandbox:
  `world.appointments['C-1042'].slot.weekday < 5` (ISO 8601 strings expose `year`, `month`,
  `day`, `hour`, `minute`, `second`, `weekday` with Monday = 0, and `date`). Names: `world`
  (= `world_after`), `world_before`, `facts`. Functions: `len any all min max sum abs round sorted
  int float str bool keys values`.
- Judges: `resolved`, `no_hallucination`, `policy_compliance`, `tone`, `handoff_appropriate`,
  `pii_leak` (`src/hecklr_grade/rubrics/`). A judge's version is a hash of its prompt.
- Custom graders register a zero-argument factory under the `hecklr.graders` entry-point group.

Semantics of `ok` / `skipped` / `error` grades: ADR 0004a. Part of [Hecklr](../../README.md);
owned by WS4, see `docs/SPEC.md`.
