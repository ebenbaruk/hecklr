# hecklr-world

Hecklr world sandbox: stateful mock backend exposed over MCP and HTTP.

Part of [Hecklr](../../README.md). Owned by WS3; see `docs/SPEC.md` and
`docs/adr/0003a-world-sandbox-server.md`.

```python
from hecklr_world import Sandbox, diff_snapshots, load_world

world_def = load_world(Path("worlds/acme-maintenance.yaml"))  # seed files inlined

async with Sandbox(tool_mode="sandbox") as sandbox:  # one localhost server for the whole run
    world = await sandbox.open(brief, world_def, sink)  # a WorldSession for one trial
    world.mcp_url  # http://127.0.0.1:<port>/t/<trial_id>/mcp
    world.http_url  # http://127.0.0.1:<port>/t/<trial_id>  (+ /openapi.json, /tools/<name>)
    before = world.snapshot()
    ...  # the agent calls tools; every call is a tool_call + tool_result event on `sink`
    changes = diff_snapshots(before, world.snapshot())
    await world.close()
```

- In-process agents and tests can call `await world.call_tool(name, args)` directly, or build a
  `TrialWorld(brief, world_def, sink)` without a server.
- `Sandbox(tool_mode="observed")`: sessions have no URLs. Point the agent's OTLP exporter at
  `sandbox.otlp_endpoint`. Its GenAI `execute_tool` spans become `source="observed"` events.
- Python tools are `fn(store: Store, **params) -> JSON` and raise `ToolError` subclasses
  (`NotFoundError`, `ConflictError`, ...) to fail a call.
