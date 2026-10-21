# hecklr-core

Hecklr orchestrator: loads `hecklr.yaml`, expands the trial matrix, enforces budgets, runs trials
concurrently through the trial-loop Protocols, and stores results.

Part of [Hecklr](../../README.md). Owned by WS1; see `docs/SPEC.md` and ADR 0001a.

```python
project = load_project(Path("hecklr.yaml"))
plan = plan_run(resolve_suite(project, "ci"), git_sha=sha)  # refuses an over-budget estimate
async with (
    open_llm(project.config, project.output_dir) as llm,  # cache + rate limits + router
    ResultStore(project.output_dir / RUNS_DIR) as store,
):
    run = await RunExecutor(plan.suite, collaborators, store).execute(plan.run)
```

Results land in `.hecklr/runs/<run_id>/`: `results.duckdb` (append-only tables `runs`, `trials`,
`events`, `grades`; view `runs_latest`), `trials.parquet`, `events.parquet`, `grades.parquet` and
`run.json`. `ResultStore.load_run(run_id)` returns the typed models exactly as written.
