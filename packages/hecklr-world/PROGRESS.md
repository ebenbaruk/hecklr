# hecklr-world progress

Owner: WS3. Spec: `docs/SPEC.md` → "World sandbox (`hecklr-world`)", "Core data model", "Agent
adapters" (`SessionContext`, `X-Hecklr-Trial-Id`). Contracts: ADR 0004 (declarative tools), ADR 0006
(trial loop), `hecklr_schemas.world` / `hecklr_schemas.trial`. Design: ADR 0003a.

## Done

- `Store` (`store.py`): per-trial in-memory collections, deep-copied from the `World` on open, so
  reads and writes never share references. CRUD validates against `record_schema`. Generated keys
  are `"{collection}-{n}"`, which is deterministic. Bad seed data fails at open.
- `Toolbox` (`tools.py`): validates arguments against each tool's JSON Schema (Draft 2020-12) and
  runs `get` / `list` / `find` / `create` / `update` / `delete` and `PythonOp`
  (`fn(store, **params)`, resolved with importlib when the trial opens).
- `ToolError` family (`errors.py`): structured `{"error": {"code", "message"}}` with an HTTP
  status per code. `WorldConfigError` is for harness-side problems.
- `FaultPlan` (`faults.py`): one RNG per tool seeded from `sha256(seed:tool)`, so the agent's
  interleaving cannot shift a schedule. Supports `probability`, `after_calls`, and first-match-wins.
  Kinds: `timeout` (held until `close()`), `http_500`, `slow`, `stale_data`.
- `TrialWorld` (`session.py`, the `WorldSession`): a direct `call_tool` API that emits
  `tool_call` (actor `agent`) and `tool_result` (actor `tool`, `latency_ms`, `fault`) for every
  call. Unknown tools and abandoned or held calls are recorded too. A crashing Python tool returns
  a 500 to the agent and writes an `error` event. Also provides `snapshot()` and observed-mode
  `record_observed`.
- `diff_snapshots` (`diff.py`): sorted added / removed / changed paths in state-expression syntax.
- `load_world` (`loader.py`): YAML world files, with `seed_file` (JSON / YAML / CSV) inlined.
- `Sandbox` (`sandbox.py`, the `WorldSandbox`): one in-process uvicorn + FastAPI server on an
  ephemeral `127.0.0.1` port serves every trial under `/t/{trial_id}`. It exposes the OpenAPI tool
  routes plus a per-trial `openapi.json`, MCP (official SDK, stateless streamable HTTP), and OTLP/HTTP
  JSON at `/v1/traces` for observed mode (`app.py`, `otlp.py`).
- Tests: 90 unit/integration tests (`uv run pytest packages/hecklr-world -n auto`), with 99% line and
  branch coverage (780 statements, 0 missed). Evidence by requirement:
  - HTTP server with a real client: `test_server_http.py`. It uses httpx2 against the live
    localhost server and covers the OpenAPI document, state changes, structured errors,
    404 for closed or unknown trials, the `http_500` and `timeout` faults, and teardown.
  - MCP with the official SDK client: `test_server_mcp.py`. It covers list_tools, call_tool,
    errors and faults as `isError`, isolation, unknown trials, and no redirect on `mcp_url`.
  - Faults, every kind: `test_faults.py`. It covers determinism by seed, probability, `after_calls`,
    per-tool streams, and cancelled held calls.
  - Isolation property (Hypothesis): `test_isolation.py::test_two_concurrent_trials_never_see_each_others_writes`,
    plus `test_a_trial_replays_identically_from_its_seed`. Over HTTP:
    `test_thirty_two_concurrent_trials_are_isolated_over_http`.
  - Observed mode with synthetic OTLP payloads: `test_observed.py`. It covers the four join keys,
    the error status, rejected spans, the 415 for protobuf, and the AnyValue kinds.
  - Live (`-m live`, local Ollama `qwen3:4b-instruct`):
    `test_live_ollama_agent.py::test_live_model_reschedules_through_the_mcp_sandbox` passes. A
    real model discovered the tools over MCP and rescheduled the visit. The diff shows
    `appointments['A-1'].slot` changed.
- Manual run: the server was started and driven with curl. Results:
  - The OpenAPI document was served, a reschedule returned 200, a bad slot returned 400, an unknown
    customer returned 404, and the `slow` fault took 0.30 s.
  - The Python handoff and a raw MCP JSON-RPC POST both worked.
  - In observed mode, an OTLP span posted with curl became observed events, and an unattributed
    span was reported as rejected.
  - Each trial's diff showed only its own writes.

## Next

- WS7 composition: `async with Sandbox(tool_mode=config.agent.tool_mode)`, then
  `load_world(config_dir / suite.world)` once per run, then `await sandbox.open(brief, world, sink)`
  per trial. Pass `world.mcp_url` and `world.http_url` into `SessionContext`. Store
  `diff_snapshots(before, after)` for the report.
- WS8: the acme-maintenance world can use the same ops as `tests/fixtures/example_world.yaml`.

## Open questions

- Adapters (WS6) may also surface tool events from the agent's stream (`AgentChunk.tool_call`).
  With the sandbox, the sandbox's events are authoritative, so WS1/WS6 should avoid double-logging
  sandbox calls.
- Observed spans use the agent host's wall clock. On one host the skew is zero, and latency comes
  from the span's own duration either way.

## Decisions

- ADR 0003a: one shared server routed by trial id; a per-trial OpenAPI document; stateless MCP;
  one error model; the `PythonOp` signature; observed-mode join keys; sandbox-mode trials reject
  spans.
