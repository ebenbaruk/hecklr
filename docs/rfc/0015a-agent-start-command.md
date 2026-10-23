# RFC 0015a. `agent.command`: how to start the agent under test

Date: 2026-10-23 · Status: proposed · Workstream: WS15

## Problem

`hecklr fix` must run every candidate patch against an agent that reads the patched files, so it
starts the agent itself, once per pass, from a copy of the project (ADR 0015a). `hecklr.yaml`
says how to reach the agent (`agent.adapters`) but not how to start it, and `AgentConfig` is
frozen with `extra="forbid"`, so the start command can only be given as `hecklr fix --agent-cmd`
today. Users must repeat it on every invocation, and CI and the UI cannot read it from the
project.

## Proposed change

Add one optional field to `hecklr_schemas.AgentConfig`:

```python
command: str | None = None
"""Starts the agent from the project directory: ``{port}`` (required), ``{python}``,
``{project}``; e.g. ``{python} agent/serve.py --port {port}``. Used by ``hecklr fix``."""
```

`hecklr fix` would use `--agent-cmd` when given, else `agent.command`, else fail as it does now.
The placeholders and the process contract are ADR 0015a's.

## Alternatives

- A sidecar file (`hecklr-fix.yaml`): a second configuration file for one field.
- An environment variable: invisible in the project, easy to get wrong in CI.
- Keep `--agent-cmd` only (the current state): works, but repeats a project fact on every call.

## Migration

None: the field is optional and `schema_version` stays 1. Until it is accepted, `--agent-cmd` is
the only way, and it keeps working afterwards.
