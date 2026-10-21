# 0003a. World sandbox: one shared server, routed by trial id

Date: 2026-10-21 · Status: accepted · Workstream: WS3

## Context

`hecklr-world` implements `WorldSandbox` / `WorldSession` (ADR 0006) over the declarative tools of
ADR 0004. A run executes 32+ trials at once, each with its own world state, and the agent reaches
its tools over MCP, over an OpenAPI HTTP API, or (observed mode) not at all, in which case its
OpenTelemetry spans are the only record. The spec leaves the serving topology, the wire shapes and
a few semantics open.

## Decision

- **One server per run.** `Sandbox` (an async context manager) runs one FastAPI app under uvicorn,
  in the harness process, on an ephemeral `127.0.0.1` port. Every trial lives under
  `/t/{trial_id}`: `POST /t/{id}/tools/{tool}`, `GET /t/{id}/openapi.json` and the MCP endpoint
  `/t/{id}/mcp`. `http_url` is `/t/{id}` and `mcp_url` is `/t/{id}/mcp`. The embedded uvicorn
  installs no signal handlers and no logging config (the harness owns both).
- **Per-trial OpenAPI document**, generated from the world's tools (OpenAPI 3.1, so each tool's
  JSON Schema is used verbatim), not FastAPI's own document. Worlds can differ per scenario, so
  the generic route cannot describe them.
- **MCP via the official SDK's low-level `Server`** behind one `StreamableHTTPSessionManager` in
  stateless JSON mode. The handlers read the trial id from the mount's path parameter, so no MCP
  session state is shared between trials. The SDK does not validate tool arguments; we do.
- **One validation and error path for every transport.** Arguments are validated against the
  tool's JSON Schema, then records against `record_schema`. Failures are `ToolError`s with a code
  and an HTTP status (`invalid_arguments` 400, `unknown_tool` / `not_found` 404, `conflict` 409,
  `internal_error` 500, `timeout` 504). HTTP returns `{"error": {"code", "message"}}`, and MCP
  returns the same JSON with `isError`. A crashing Python tool, or one that returns a value that
  is not JSON, is a 500 to the agent plus an `error` event (`source="world"`) and a log line. A
  Python tool may raise any `ToolError`, `ToolTimeoutError` included, and it is recorded like any
  other failed call.
- **`PythonOp` signature**: `fn(store, **params) -> JSON`, where `store` is the trial's `Store`.
  Entry points resolve with importlib when the trial opens, so a bad entry point fails the trial
  as a harness error before the conversation starts. `create` without a key generates
  `"{collection}-{n}"`, which is deterministic.
- **Faults.** Each tool gets its own RNG stream seeded from `sha256(seed:tool)`, so the agent's
  interleaving of different tools cannot shift a schedule. A `timeout` call is held until
  `close()`. A `slow` call still sleeping at `close()` is released there too and never writes to
  the store. `close()` records each released call's `tool_result` (error `timeout`, the injected
  fault kind) before it returns, so the result lands before the "after" snapshot. The frozen
  `WorldSession` Protocol calls such calls "dropped": here they are dropped for the agent but still
  recorded, so every `tool_call` has exactly one `tool_result` and WS1 must not add its own. A call
  the client abandons is recorded once as well.
- **Observed mode** is a sandbox-wide `tool_mode`. Its sessions have no URLs, and spans reach
  `POST /v1/traces` (OTLP/HTTP JSON; protobuf gets 415). An `execute_tool` span joins a trial
  through the first of: span attribute `hecklr.trial_id`, captured header attribute
  `http.request.header.x-hecklr-trial-id`, resource attribute `hecklr.trial_id`, or the export's
  `X-Hecklr-Trial-Id` header. Span wall-clock times map onto the trial clock through the wall time
  recorded when the session opened. Spans for sandbox-mode trials are rejected (reported in
  `partialSuccess`) so a call is never counted twice. OTLP exporters retry, so a session records
  each `call_id` (the span's `gen_ai.tool.call.id`, else its span id) once.
- **`load_world(path)`** inlines `seed_file` records (JSON, YAML, CSV), so the sandbox never
  touches the file system. A `World` that still names a seed file is refused.

## Consequences

- One port and one event loop serve the whole run. Declarative tool calls are in-memory
  operations, so they never block the loop for long. Python tools run synchronously on that same
  loop, so a slow or blocking Python tool (network I/O, `time.sleep`, heavy compute) stalls every
  concurrent trial. Tool authors must keep them fast and pure; simulated latency belongs in a
  `slow` fault, not in the tool.
- Agents behind the sandbox need nothing Hecklr-specific beyond the URL, which is already in
  `SessionContext`.
- Observed-mode timestamps inherit any clock skew between the agent host and the harness. On one
  host that skew is zero, and latency is unaffected because it comes from the span's own start and
  end.
