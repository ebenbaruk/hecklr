# 0016a. Local UI: read-only results API, review files and packaged assets

Date: 2026-10-22 · Status: proposed · Workstream: WS16

## Context

`hecklr ui` (spec, "Local UI") is a FastAPI backend over the stored runs plus a React app, with
six views: runs list, comparison, trial viewer, failure clusters, scenario approval queue and
judge labeling queue. It is "read-only on results except labels and approvals". The spec leaves
open how the UI reads runs another process may be writing, where human labels and approval
decisions are stored, where the miner's clusters and proposals live (WS13 is built in parallel),
and how a Python wheel ships a Node-built app.

## Decision

- **Reading runs never locks them.** ADR 0001a: a run's DuckDB has one writer and readers use
  `run.json` plus the Parquet exports. The UI lists runs from `run.json` and loads a run from its
  Parquet exports (written when the run ends); without exports (a killed run) it opens
  `results.duckdb` with `read_only=True`. A run still being written answers `409` ("in
  progress") and the list shows its status from `run.json`. The UI never opens a run database
  for writing.
- **Statistics are the reports' statistics.** Every number comes from `hecklr.results`
  `analyze_results` (`hecklr_report.analyze`) or `hecklr_stats.calibrate_judge`, so the UI and
  `hecklr report` / `hecklr calibrate` can never disagree. Analyses are cached in memory per
  (candidate, baseline, `run.json` mtime); the runs list fetches each row's summary lazily.
- **Human labels:** `<output_dir>/labels.jsonl`, one `HumanLabel` per line, append-only; the
  newest per (grade, labeler) wins (the schema's rule). It is exactly the file
  `hecklr calibrate --judges --labels .hecklr/labels.jsonl` reads. The queue samples `ok` judge
  grades (`judge:*`) of the chosen runs, stratified by rubric: each rubric's grades are shuffled
  with `random.Random(seed)` (seed = the run seed, or `stats.seed`) and interleaved round-robin,
  unlabeled ones first. The queue is a fixed sample: the first `limit` (default 50, US-11) of
  that order, so labeling it does not draw new grades in; it changes only when runs are added
  or `limit` is raised (`?limit=` on the API). A fixed, seeded sample keeps the agreement
  estimate's sample from depending on the order a person labels in. The judge's own verdict is hidden until the human answers, so it cannot
  anchor the label. Agreement uses `calibrate_judge` per (rubric, version, judge model).
- **Miner output (contract with WS13):** under `<output_dir>/mine/` (any depth), `clusters.jsonl`
  holds `FailureCluster` records and `proposals.jsonl` holds `ScenarioProposal` records, one JSON
  object per line, append-only, newest record per `id` wins. An optional `conversations.jsonl`
  (`Conversation` records, already PII-scrubbed) next to them lets production clusters show
  their exemplars. This matches WS13's ADR 0013a (`mine/<mine_id>/` per mining run, the queue
  is the union of every `proposals.jsonl`, last record per id wins, a decision is appended to
  the proposal's own file). Like the miner, the UI appends each record as one `write` on an
  `O_APPEND` descriptor, so the CLI and the UI never interleave lines. Once `hecklr-mine` is
  merged, `ui_api/review.py` can delegate reading and deciding to `hecklr_mine.load_queue` /
  `decide`.
- **Approval.** Approving (optionally with edited YAML) validates the scenario with
  `hecklr_core.load_model` (file and line in errors), checks its world is the project's world,
  its personas exist in the persona library `personas/` (the only place `resolve_suite` resolves
  a scenario's own persona ids; a suite's `personas` glob replaces them rather than extending
  it), and its id is unused in every file matched by `scenarios/**/*.yaml` or any suite's
  `scenarios` glob (one duplicate id breaks `resolve_suite` for every suite seeing both files),
  then writes `scenarios/mined/<id>.yaml` (never overwriting) and appends the decided proposal (reviewer,
  note, `decided_at`, the approved scenario) to the proposal's own `proposals.jsonl`. Rejecting
  appends the decision only. A decided proposal cannot be decided again (`409`). When no
  suite's `scenarios` glob covers the written file, the response carries a warning naming the
  globs, since that scenario would otherwise never run.
- **Who.** The labeler and reviewer name defaults to `--user` (else the OS user name); the UI
  lets a person change it. It is a label, not authentication: the server binds `127.0.0.1` by
  default and is a local single-user tool (spec non-goal: no hosted SaaS).
- **Local-only guard.** No login, so the server answers only `Host: localhost`, `127.0.0.1` or
  `[::1]` (plus `--host` when it names an interface) and refuses others with `400`
  (`TrustedHostMiddleware`): a DNS-rebinding page cannot read results or append labels and
  decisions. Cross-origin JSON POSTs are already stopped by the CORS preflight (no CORS headers
  are sent) and non-JSON bodies fail validation. A wildcard bind (`0.0.0.0`, `::`) is a
  deliberate choice to serve the network and accepts any host name.
- **Assets.** Vite builds `ui/` into `packages/hecklr/src/hecklr/ui_api/static/` (git-ignored);
  hatch ships it as a wheel artifact, so `pip install "hecklr[ui]"` + `hecklr ui` needs no Node.
  `hecklr ui --dev` serves only the API and `npm run dev` proxies `/api` to it. Without built
  assets `hecklr ui` exits with the command that builds them.
- **Audio.** Recordings are served from the run directory only (paths escaping it are refused,
  as in the HTML report) with HTTP range support; wavesurfer.js decodes and draws them, and the
  turn markers come from the trial's `audio_span` and `interrupt` events.

## Consequences

- A run in progress is visible but not inspectable until it ends; acceptable for a local tool.
- Labels and decisions are plain JSONL a person can read, diff and commit, and the CLI reads them.
- Releases must run `npm ci && npm run build` in `ui/` before `uv build`; the CI UI job builds the
  app and the Python tests cover the static serving with a fixture directory.
