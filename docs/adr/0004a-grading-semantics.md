# 0004a. Grading semantics: binding, outcomes, state sandbox and judge versions

Date: 2026-10-21 · Status: proposed · Workstream: WS4

## Context

The frozen `Grader` Protocol passes one `GradeContext` per grade, with an optional `criterion`.
Several questions were left to `hecklr-grade`: how a grader learns which criterion it answers, when
a grade is `ok`-and-failed versus `error` versus `skipped`, how state expressions read ISO
timestamps (ADR 0004 says the evaluator does it), how a judge's version tracks its prompt, and
which events the voice metrics read. Statistics, reports and judge calibration all depend on the
answers.

## Decision

- **Binding.** Built-in graders are stateless per family and read `ctx.criterion`.
  `build_graders(scenario)` returns `BoundGrader`s (themselves `Grader`s) that pair a grader with
  its criterion, index and phase. `grade_trial` rebinds the context per grader and runs the phases
  in order: deterministic, then judges and custom graders, then voice. Graders in one phase run
  concurrently. A grader that raises becomes an `error` grade. Bad regexes, unparsable state
  expressions, unknown rubrics and missing custom graders fail in `build_graders`, before any
  trial runs.
- **Outcomes.**
  - `ok` with `passed=False`: the agent's behaviour failed the check. This includes a state
    expression that reads missing or malformed data (a deleted record, a non-ISO slot from F2).
  - `error`: no verdict could be reached. Examples: an invalid or escaping expression, no judge
    model configured, a judge that answers with garbage or cites unknown event ids, or a provider
    failure.
  - `skipped`: the criterion does not apply. Examples: a voice metric on chat, `args_match` when
    the tool was never called, a metric with no samples.
  - `trial_passed` is true only if at least one `ok` criterion grade exists and all of them pass.
- **State sandbox.** The sandbox uses simpleeval with these restrictions:
  - The attribute handler is replaced. World data is exposed as read-only `JsonObject` views, and
    attribute access on a view is only a key lookup.
  - Other types use an allow-list: six string methods, plus `year`, `month`, `day`, `hour`,
    `minute`, `second`, `weekday` and `date` on `IsoDateTime`. Any string that parses as ISO 8601
    becomes an `IsoDateTime`.
  - Only pure functions are allowed. simpleeval's `rand` is not provided.
- **Judge versions.** Rubrics are package files. `grader_version` is a 12-hex content hash of the
  system prompt, the transcript format version, the verdict schema, the rubric name, the
  world-access flag and the rubric text. A single judge runs at temperature 0. A 3-judge majority
  runs at 0.7 with seeds derived from (trial seed, rubric, vote index). Any unusable vote makes the
  whole grade an `error`.
- **Voice.** Metrics read only `utterance`, `audio_span` (speech), `interrupt` and `silence`
  events, as written on `VoiceMetric`. Quantiles are nearest-rank. A barge-in the agent never
  reacted to counts from the barge-in until the agent's speech ended, or until the call ended if
  no span covers it.
- **Diagnostics.** Voice scenarios get every uncovered voice metric as a diagnostic, gated on the
  spec's typical gates. Email scenarios get `email_threading`. Diagnostics never decide a trial.

## Consequences

- Statistics can read `ok` grades only. Error and skip counts stay visible and are never passes.
- Editing a rubric changes its version automatically, so κ (`HumanLabel.grader_version`) never
  mixes prompt versions. Editing transcript rendering requires bumping
  `TRANSCRIPT_FORMAT_VERSION`.
- Safety graders assume a `refunds` collection for their state cross-check, which applies only
  when the world has one. They also assume a `caller_fact` scenario fact to identify the caller.
  Without that fact, every customer is treated as someone else.
