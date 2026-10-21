# hecklr-sim

Hecklr simulated customer: scripted, guided, replay and adversarial modes.

Part of [Hecklr](../../README.md). Owned by WS2; see `docs/SPEC.md` and
`docs/adr/0002a-simulator-decisions-and-consistency-guard.md`.

```python
from hecklr_sim import make_simulator, transcripts_to_scenarios

factory = make_simulator("ollama/qwen3:4b-instruct")  # a SimulatorFactory
simulator = factory(brief, llm)  # per trial: a CustomerSimulator
move = await simulator.next_turn(dialogue)  # SimTurn | SimEnd
simulator.contradictions  # turns the guard regenerated

# `hecklr replay FILE`: real transcripts -> replay scenario YAML
await transcripts_to_scenarios(path, out_dir, llm, model=..., world="acme", persona="caller")
```

| Mode | Behavior |
| --- | --- |
| `scripted` | Sends `user_turns` in order; no model calls |
| `guided` | LLM pursues the goal with a private brief; patience from traits; structured end reasons |
| `replay` | Goal and grounded facts extracted from a real transcript, then run as `guided` |
| `adversarial` | `guided` plus a hidden attack strategy (`BUILTIN_STRATEGIES`, extensible) |

Tests drive it with `FakeLLM`; `hecklr_sim.testing.decision_reply` builds simulator replies.
