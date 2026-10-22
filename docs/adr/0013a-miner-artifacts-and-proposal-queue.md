# 0013a. Miner: artifacts on disk and the scenario-proposal review queue

Date: 2026-10-22 · Status: proposed · Workstream: WS13 (consumed by WS16, the UI)

## Context

`hecklr mine LOGS` turns production logs into failure clusters and proposed scenarios that a
person approves in the UI (spec → "Miner", US-08, "Local UI": "scenario approval queue",
"read-only on results except labels and approvals"). The frozen schemas define
`FailureCluster` and `ScenarioProposal`. A proposal is reviewed by re-appending it with the same
`id` (the newest record wins). Run results live in a per-run DuckDB file (ADR 0001a), but a
mining run is not a run: it has no trials, and the CLI and the UI server must both write to the
queue. DuckDB allows one writer process per file.

## Decision

- **One directory per mining run**: `<output_dir>/mine/<mine_id>/` (`output_dir` defaults to
  `.hecklr`; `mine_id` is a ULID, so directories sort by creation time). `hecklr mine --out DIR`
  writes to `DIR` instead. Files, all UTF-8, JSON Lines with one model per line:

  | File | Content |
  | --- | --- |
  | `summary.json` | `hecklr_mine.MineSummary`. Written last, so its presence marks a complete run |
  | `conversations.jsonl` | Every ingested `Conversation`, PII-scrubbed, in input order |
  | `failures.jsonl` | `hecklr_mine.FailureReport` per failed conversation: `signals` (kind, turn, detail), `summary` (the text that was embedded), `cluster_id` (`null` = noise), `judge_errors` |
  | `clusters.jsonl` | `FailureCluster` (`source: production`), largest first. Exemplars are the members closest to the centroid |
  | `proposals.jsonl` | `ScenarioProposal`, **append-only**. This is the queue |
  | `scenarios/<scenario id>.yaml` | Each proposed scenario as authored YAML, for reading and diffing |

- **The queue** is the union of every `mine/*/proposals.jsonl`. The current state of a proposal is
  the last line with its `id`, across files (`hecklr_mine.load_queue`). A proposal never moves
  between files.
- **A review** appends a decided copy (`status: approved | rejected`, `reviewer`, `decided_at`,
  optional `note`) to the file that holds the proposal. Python callers, including the UI's
  FastAPI server, use `hecklr_mine.decide(mine_root, proposal_id, status, reviewer=...,
  note=..., scenarios_dir=...)`. It refuses to decide a proposal twice. With `scenarios_dir`, an
  approval also writes `<scenarios_dir>/<scenario id>.yaml`. That puts the scenario in the suite,
  and an existing file is never overwritten. The suggested `scenarios_dir` is
  `<project>/scenarios/mined`, so `scenarios/**/*.yaml` suite globs pick it up.
- **Concurrent writers**: each record is one `os.write` of a full line on a file opened with
  `O_APPEND`, so the CLI and the UI never interleave partial lines. The last complete line wins.
  Readers ignore blank lines and fail loudly, with file and line, on a corrupt one.
- **Linking back**: `ScenarioProposal.cluster_id` → `clusters.jsonl` of the same directory.
  `FailureCluster.conversation_ids` and `exemplar_conversation_ids` → `conversations.jsonl`.
  `Scenario.source` is `{kind: mined, cluster_id, conversation_id: <exemplar>, note: <label>}`.
- **Deduplication**: new proposals are compared, by embedding the title and goal, with the
  suite's scenarios and every queued proposal that is not rejected. A rejected proposal can
  therefore come back if the failure persists. Dropped duplicates are listed in
  `summary.json` (`duplicates`).

## Consequences

- No database server or lock file. The UI lists runs with a directory glob and can still query
  everything with DuckDB's `read_json('…/mine/*/clusters.jsonl')` if it wants SQL.
- History is complete: every decision stays in the file, so the UI can show who decided what and
  when.
- `O_APPEND` single-write atomicity is a POSIX guarantee on local filesystems. On network
  filesystems two simultaneous reviews could interleave. Reviews are rare, human-paced events,
  so this is accepted.
- `decide` checks the current state and then appends, without a lock. Two reviewers deciding the
  same proposal at the same moment (CLI and UI) can both see `proposed` and both append; the last
  line wins. Two approvals cannot write the scenario twice (the file is created with `open("x")`),
  but an approval racing a rejection can leave the scenario file written while the record says
  `rejected`. Reviews are human-paced, so the window is accepted; the UI should refresh the queue
  after each decision.
- `summary.json` also lists `scrub_errors` and `summary_errors` (conversation id -> why the LLM
  scrub or LLM summary fell back). Both default to empty, so older summaries still read.
- The format is versioned by the frozen schemas (`schema_version` on every record) and by
  `MineSummary.schema_version`. Changing a file's layout requires a new ADR.
