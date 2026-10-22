# Local UI (`hecklr ui`) progress

Owner: WS16. Spec: `docs/SPEC.md` → "Local UI", US-03, US-08, US-11, "Judge calibration".
Decisions: ADR 0016a. Backend: `packages/hecklr/src/hecklr/ui_api/`, command
`packages/hecklr/src/hecklr/commands/ui.py`. Frontend: this directory.

## Plan

1. Backend read side: read-only run loading (Parquet / read-only DuckDB), runs list, run
   analysis, comparison, trial detail (events, grades, world diff, recordings with markers).
2. Backend review side: labels (`labels.jsonl`, queue, agreement κ), miner files (clusters,
   proposals), approval writing `scenarios/mined/<id>.yaml`.
3. `hecklr ui` command, static serving, wheel packaging of the built app.
4. React app: runs, compare, trial viewer (wavesurfer), clusters, approvals, labeling; dark mode.
5. CI job for `ui/`; real store from the acme-maintenance smoke suite; browser check.

## Done

(filled in as steps land, with test names and numbers)

## Open questions

- WS13's miner output layout: built against the contract in ADR 0016a with fixtures.
