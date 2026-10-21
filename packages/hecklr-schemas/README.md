# hecklr-schemas

Frozen data models and Protocols shared by every Hecklr package:

- authored files: `Scenario`, `Persona`, `World`, `HecklrConfig`, and `Conversation` (native JSONL);
- produced artifacts: `Run`, `Trial`, `Event`, `Grade`, `Estimate`, `MetricSummary`,
  `Comparison`, `FailureCluster`, `ScenarioProposal`, `HumanLabel`, `Patch`;
- Protocols: the trial loop (`CustomerSimulator`, `WorldSandbox`, `ChannelDriver`, `EventSink`),
  adapters, graders and LLM providers;
- the harness clock.

Part of [Hecklr](../../README.md). Owned by WS0; frozen after Wave 0 (changes need an RFC in
`docs/rfc/`). See ADRs 0002 (typed events), 0004 (world tools) and 0006
(trial loop and measurement definitions) in `docs/adr/`.

```python
from hecklr_schemas import Scenario, authoring_json_schemas

scenario = Scenario.model_validate(yaml_data)  # typos fail fast: extra="forbid"
schemas = authoring_json_schemas()  # for editors and validation
```
