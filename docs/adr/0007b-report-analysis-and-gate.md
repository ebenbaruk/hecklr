# 0007b. Reports: from stored results to verdicts, flips, latency deltas and exit codes

Date: 2026-10-21 · Status: proposed · Workstream: WS7b

## Context

`hecklr-stats` computes intervals and pass-rate comparisons from `TrialOutcome` records, but
nothing turned stored results (`RunResults`: trials, events, grades) into those records. The
spec also leaves open several things that every report format needs to agree on: when a trial
with an `error` grade counts, what a "flipped" scenario and its "cause" are, how latency is
compared between runs (ADR 0005b defers it), when a JUnit test case fails, and when
`hecklr gate` exits 3. WS7a and WS7b froze the `analyze` / `render_*` / `gate_exit_code` API.

## Decision

- **One analysis, many renderers.** `analyze(candidate, baseline, stats, scenarios)` returns a
  frozen, JSON-serializable `Analysis` (`schema_version` 1). Renderers only format it; the HTML
  report also reads the stored run for the per-trial viewer. Two keyword-only extras, `personas`
  (for SNR and accent slices) and `judge_calibrations` (for κ trust flags), keep the frozen
  positional signature.
- **Trial verdicts.** Harness errors are excluded and counted. Agent errors and timeouts fail.
  A failing `ok` criterion grade fails the trial even if another grader errored. A trial whose
  only non-passing criteria are `error` grades is **undecided**: excluded and counted, because
  calling it a pass or a fail would be a guess. Otherwise `hecklr_grade.trial_passed` decides.
- **Slices.** Labels are `channel`, `persona` and, for voice trials with a known persona,
  `snr_db` (buckets clean, 20, 10, 5, 0 dB; a level falls in the highest bucket it reaches,
  negative levels count as 0) and `accent`. Tags come from the scenario.
- **Flips and causes.** A scenario passes on a side when at least half its analyzed trials pass
  (the rule of `hecklr_stats.allocate_trials`). A flip is passing on one side and not the other.
  Its cause is the most frequent failure on the failing side: the failing criterion's label
  (`judge:resolved`, `tool_called reschedule_appointment`, `state: <description>`, ...) or the
  trial status. Flips are grouped by (direction, cause) for US-05.
- **Latency comparisons.** p50, p95 and p99 of agent response latency (chat TTFT, voice response
  latency, ADR 0006) on the whole run and per channel. Each run is bootstrapped by whole trials,
  independently (latencies are not paired). The p-value inverts the percentile interval (twice the
  smaller tail mass of Δ around 0), so "p < α" matches "the 1 - α interval excludes 0". The
  minimum effect is relative: `stats.min_effect` × the baseline quantile. Increases are the
  regression direction. Channel slices are BH-adjusted per quantile.
- **Gate.** The suite pass-rate verdict decides (ADR 0005b). Latency and slice verdicts are
  diagnostics: a latency gate belongs in scenarios as `latency_p95_below` or `voice_metric`, which
  already feed the pass rate. Exit 3 when the analysis lists a problem (run failed, cancelled or
  unfinished; nothing analyzable; no shared scenario) or there is no baseline comparison.
- **JUnit.** A scenario's test case fails when fewer than half its analyzed trials passed (the
  same majority rule as flips); it is an `error` when no trial could be analyzed.
- **PR comment.** Each section has a line budget (at most 27 lines in total), with "+N more" for
  the rest. User and model text is escaped; causes print as code spans.
- **HTML.** One file: inline CSS and JavaScript, Jinja2 autoescaping, script data through `tojson`.
  The waveform is drawn on a canvas from peaks computed in Python (no wavesurfer.js to vendor).
  Recordings are embedded as `data:` URIs, failed trials first, until 8 MB are spent; the rest are
  linked relative to the report.

## Consequences

- The CLI (WS7a) must pass `personas=` for voice slices to appear; without it the voice section
  still shows turn-taking metrics but no degradation curve.
- Undecided trials shrink the sample instead of biasing it; their count is in every report.
- A latency regression never fails `hecklr gate` by itself. If teams want that, `GateConfig`
  needs a field, which is a schema change (RFC).
- `Comparison.min_effect` holds milliseconds for latency comparisons and a proportion for pass
  rates; renderers read the unit from the metric name.
