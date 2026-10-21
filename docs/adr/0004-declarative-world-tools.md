# 0004. Declarative world tools: one CRUD operation or a Python entry point

Date: 2026-10-21 · Status: accepted · Workstream: WS0

## Context

The world sandbox (`hecklr-world`, WS3) is a stateful mock backend that agents call through MCP and
an OpenAPI server; scenarios assert on its state. The spec leaves the tool format open. The
reference world ("acme-maintenance": customers, appointments, invoices and refunds, policies,
emergency handoff) needs lookups, filtered lists, inserts and updates, plus a little custom logic.
A general rules language would be another framework to build, document and test.

## Decision

- A `World` is a set of named collections (records keyed by one field, inline seed records or a
  seed file, optional JSON Schema) and a list of tools.
- A tool has a name, a description, a JSON Schema for its parameters and exactly one operation:
  - `CollectionOp` with `kind` in `get`, `list`, `find`, `create`, `update`, `delete` over one
    collection. Tool parameters map to record fields by name; `key_param` names the parameter
    carrying the record key (defaults to the collection key).
  - `PythonOp` with an `entrypoint` (`module:function`) for anything else.
- Fault injection is per scenario (`Scenario.faults`), not per world: `timeout`, `http_500`, `slow`
  (`delay_ms`), `stale_data` (`patch` overrides returned fields), with a probability drawn from the
  trial's seeded RNG and an optional number of healthy calls first.
- Snapshots are plain JSON: collection → record key → record, which is what state expressions index
  (`world.appointments['C-1042'].slot`). Converting ISO timestamps to objects with `.weekday` and
  `.hour` is the evaluator's job (WS4), not the snapshot's.

## Consequences

- Most worlds need no code, and the same definition drives MCP, OpenAPI and snapshots.
- Validation catches tools pointing at unknown collections, duplicate tool names and seed records
  missing their key at load time.
- Logic that does not fit one CRUD operation needs Python, which keeps the YAML format small.
