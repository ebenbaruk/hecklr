# acme-maintenance progress

Owner: WS8 (reference agent, chat; faults F1-F6). Spec: `docs/SPEC.md` → "Testing strategy and
quality bar" (fault matrix SC8), "Core data model", "World sandbox", "Graders", "Simulator",
"Configuration". ADRs: 0004, 0004a, 0005a, 0006, 0006a, and this workstream's 0008a.

## Done

- **World** `worlds/acme-maintenance.yaml`: 12 customers, 14 visits, 34 slots, 13 invoices,
  refunds, emergency tickets, 7 policies (cancellation is free with 24 h notice); 12 declarative
  tools, no `PythonOp`. `tests/test_world.py` (every tool runs, the catalog matches the world,
  every visit is over 24 h away, the data prober's decoys point at C-2077 with stale details).
- **Agent** `agent/acme_agent`: FastAPI `/chat` (JSON), `/chat/stream` (SSE), `/healthz`;
  `InProcessAgent` for the `python` adapter; `serve.py` / `python -m acme_agent` with `--brain`,
  `--fault`, `--overlay`, `--world-base-url` (and `ACME_*` variables).
  - LLM brain: structured `AgentStep` loop over `hecklr_llm`, prose tool catalog, seeded
    requests. `tests/test_llm_brain.py` (FakeLLM).
  - Rules brain: deterministic CI stand-in driven by the same files (`rules/behaviour.py`).
    `tests/test_rules_brain.py`, `tests/test_nlu.py`.
  - Tools: the trial's sandbox over HTTP (not re-reported) or a local world (reported over SSE).
    `tests/test_server.py` drives the server with hecklr-adapters' own `http` adapter, including
    against a real `hecklr_world.Sandbox`.
- **Faults** `../acme-faults/F1..F6`: one changed line in one editable file each, with a README.
  `tests/test_faults.py`: `test_sc8_each_fault_changes_exactly_one_line_of_one_editable_file`,
  `test_sc8_each_fault_flips_exactly_its_behaviour`, and one conversation test per fault.
- **Suite** `hecklr.yaml` (`ci` 40 core scenarios x 3, `full` 64 x 5, `redteam` 9 x 5, `smoke`
  33 scripted x 1), 64 guided scenarios in 5 areas, 33 scripted scenarios, 10 personas (8 +
  refund fraudster + data prober). `tests/test_suite.py`:
  - every suite resolves with `hecklr_core.load_project` / `resolve_suite`;
  - every criterion builds a grader (`hecklr_grade.build_graders`);
  - `test_sc8_clean_agent_passes_every_scripted_scenario` and
    `test_sc8_each_fault_fails_exactly_its_scenarios[F1..F6]`: each scripted scenario runs as a
    real graded trial in process (scripted simulator, `python` adapter, `ChatDriver`,
    `TrialWorld`, `hecklr_grade`). Clean: 33/33 pass. Each fault fails exactly its tagged
    scenarios (F1: 9, F2-F6: 6 each) and nothing else.
- **Live** (`-m live`, Ollama `mistral:latest`): `test_llm_brain_books_a_confirmed_visit` and
  `test_llm_brain_follows_the_f4_overlay`, both pass (52 s).
- Numbers: 280 tests (278 + 2 live); 95% line+branch coverage of `agent/`
  (`uv run pytest examples/acme-maintenance --cov=examples/acme-maintenance/agent`); ruff and
  `mypy --strict` clean (`MYPYPATH=examples/acme-maintenance/agent:examples/acme-maintenance/tests
  uv run mypy --strict examples/acme-maintenance/agent examples/acme-maintenance/tests`).
- Manual runs: the server was started with `serve.py` (clean and `--fault F3`) and driven with
  curl. The F3 variant quoted "€45 cancellation fee" where the clean one said cancelling is free.
  The SSE stream carried `tool_call`, `tool_result`, text deltas, `handoff` and `end`.
  The LLM brain was run on Ollama on book, gas, cancel, probe and refund flows; F3, F4 and F6
  changed its behaviour as intended on `mistral`.

## Next (integration, WS7a / integrator)

- **World URL gap.** Hecklr's `http` adapter does not send `SessionContext.world_http_url`.
  Today the agent finds its trial's tools with `ACME_WORLD_BASE_URL` + `/t/<X-Hecklr-Trial-Id>`,
  which needs the run's sandbox on a known port. `Sandbox(port=...)` exists, but `hecklr.yaml`
  cannot set it. Fix in WS6/WS7a, no schema change: add a `{{world_http_url}}` request-template
  placeholder (the agent already reads a body `world_url`) or a header. Then the fault matrix is
  one command per variant:
  `serve.py --brain rules --fault F<n>` + `hecklr run --suite smoke`, compared with the clean run.
- `scripts/mypy-all.sh` does not type-check `examples/`; add the `MYPYPATH` command above.
- Root `pyproject.toml` `testpaths` now include `examples` (one word) so CI runs these tests.

## Open questions

- Agent model: the suggested default `ollama/qwen3:8b` thinks before every step (40-250 s per
  turn locally, and it runs out of the 1024-token answer budget). `qwen3:4b-instruct` is fast but
  ignores fault prompts (F6). `mistral:latest` follows both and is the default (ADR 0008a). Cloud
  models are the spec's nightly matrix and need no change.
- Small local agents still sometimes guess a customer number before asking. This is clean-agent
  noise that the statistics must absorb. A stronger model removes most of it.

## Decisions

- ADR 0008a: faults are text only; two brains read the same files; guided plus scripted suites;
  how the agent finds its trial's world; handoff on a successful `create_emergency_ticket`.
- Personas use `en-GB` (the agent is English), not the spec example's `fr-FR`. Voice profiles
  come with WS11.
