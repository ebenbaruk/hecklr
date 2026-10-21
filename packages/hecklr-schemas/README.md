# hecklr-schemas

Frozen data models and Protocols shared by every Hecklr package: authored files (`Scenario`,
`Persona`, `World`, `HecklrConfig`), produced artifacts (`Run`, `Trial`, `Event`, `Grade`,
`Comparison`, `FailureCluster`, `Patch`), the adapter, grader and LLM Protocols, and the harness
clock.

Part of [Hecklr](../../README.md). Owned by WS0; frozen after Wave 0 (changes need an RFC in
`docs/rfc/`). See `docs/adr/0002-typed-event-payloads.md` and `docs/adr/0004-declarative-world-tools.md`.

```python
from hecklr_schemas import Scenario, authoring_json_schemas

scenario = Scenario.model_validate(yaml_data)  # typos fail fast: extra="forbid"
schemas = authoring_json_schemas()  # for editors and validation
```
