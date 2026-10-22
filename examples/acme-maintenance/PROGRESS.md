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

## Done (WS7d: integration fixes and luna, ADR 0007d)

- **World URL.** The agent resolves its tools per request: body `world_url`, the
  `X-Hecklr-World-Url` header that Hecklr's chat adapters now send, `ACME_WORLD_BASE_URL`, then
  the local copy. `/healthz` reports `model`, `fallback_tools` and `requests_by_world_source`.
  `test_server.py::test_tools_go_to_the_trial_sandbox[world_url_header|world_base_url]`,
  `test_request_world_url_and_health`.
  Real run (`serve.py --brain rules` + `hecklr run --suite smoke` over the `http` adapter): 18/33
  before the fix (all 15 booking, cancel and reschedule scenarios failed), 33/33 after, $0, with
  `requests_by_world_source.header = 80`. With `--fault F2`: 27/33, exactly the 6 reschedule
  scenarios fail. The fault matrix is now one command per variant.
- **Luna.** Default agent model `openai/gpt-6-luna`; `hecklr.yaml` simulator and judge too.
  `ACME_TEMPERATURE` (default unset: the model's own, so luna keeps reasoning). The step protocol
  says a reply ends the turn. Live (`-m live`, luna): booking, refund after verification, gas-smell
  handoff and the F4 leak all pass (4 tests, 39 s); the four flows cost $0.0014 together.
  `hecklr run --suite <15 core booking/cancel/reschedule scenarios>` fully on luna (LLM-brain
  agent over HTTP, luna simulator and judges): 15/15 passed, $0.031.
- **Flaky test.** `test_an_unreachable_sandbox_becomes_an_apology` failed when an xdist worker
  had run the hecklr pytest-plugin tests first: their `pytester.runpytest_inprocess` dropped a
  lazily imported `httpcore2` from `sys.modules`, so its re-imported exception classes escaped
  httpx2's error mapping and the agent answered 500. The root `conftest.py` now imports
  `httpcore2` up front. The test also injects the refused connection
  (`create_app(world_transport=...)`) instead of dialling port 9.
- Numbers: 279 tests + 4 live; 96% coverage of `agent/`.

## WS-m1: gate M1 fault matrix (ADR 0020a, docs/milestones/M1.md): free and A pass, B open

- `scripts/fault-matrix/fault_matrix.py`: clean, A/A and F1-F6 over HTTP, `hecklr run` + `hecklr
  gate`, per-grader paired comparisons, `matrix.json`/`matrix.md`. Tiers `free` (nightly CI,
  `.github/workflows/fault-matrix.yml`), `a` (rules brain + luna judges on `matrix`), `b` (LLM
  agent + luna simulator and judges on `matrix-live`). Tests: `tests/test_fault_matrix.py`
  (27, incl. `test_sc8_free_tier_end_to_end`; 94% coverage of the script).
- Defects the matrix found and fixed here: F1's `tool_called` checked no slot (now checks it);
  no judges in the deterministic matrix (18 judge-only twins in `scripted/judged/`); judges could
  not see today's date (world `calendar` collection); the `handoff_appropriate` rubric read tool
  latency as a late handoff (rubric clarified); F1 deleting a rule luna follows anyway (overlay now
  states the opposite rule; 6 options-first scenarios); F5 lost 7 of 8 guided scenarios to #29
  (3 more refund-fraud scenarios).
- The LLM brain counts its own spend (`/healthz spent_usd`) and caps it (`ACME_BUDGET_USD`).
- Review round 1: F1 on luna books the first slot and rebooks after the customer objects, so
  `tool_called` passed; the options-first scenarios now carry `args_match` and the matrix counts
  the spec's Tool family as one grader (`test_sc8_f1_booking_the_wrong_slot_first_regresses_the_tool_grader`).
  `/healthz failed_turns` + `check_health` refuse a run that measured a provider outage (the
  OpenAI credit ran out mid-F1). CI's `FORCE_COLOR=1` hid the run id (now stripped).
- Evidence on the final head: free tier PASS ($0), Tier A PASS ($0.1473), Tier B baseline and
  A/A valid (A/A inconclusive, no grader regressed), F1-F6 unmeasured: #45 (no OpenAI credit).

## Next (WS-m1)

- Rerun Tier B when #45 is resolved (`--tier b --budget-usd 3.2`, ~$2.8) and record it in M1.md.

## Open (owned elsewhere)

- #45 OpenAI account out of credit: blocks Tier B.
- #29 hecklr-sim guard rejects every guided scenario with a postcode and an invoice number.
- #33 all-agent-error runs pass a no-baseline gate; #34 reply size; #35 Ctrl-C traceback;
  #36 cost estimate for scripted suites; #37 judge evidence-id retry; #19 key pre-flight.

## Next (integration, WS7a / integrator)

- `hecklr run` pre-flights the simulator and judge providers (`check_model_providers`) even for
  the scripted `smoke` suite, which calls no model, so with luna in `hecklr.yaml` the free matrix
  needs `OPENAI_API_KEY` set (no money is spent). WS7c: check only the models a suite will call.
- `scripts/mypy-all.sh` does not type-check `examples/`; add the `MYPYPATH` command above.
- Root `pyproject.toml` `testpaths` now include `examples` (one word) so CI runs these tests.

## Open questions

- Local models (now the free override): `ollama/qwen3:8b` thinks before every step (40-250 s per
  turn locally, and it runs out of the 1024-token answer budget). `qwen3:4b-instruct` is fast but
  ignores fault prompts (F6). `mistral:latest` follows both (ADR 0008a).
- Small local agents still sometimes guess a customer number before asking. This is clean-agent
  noise that the statistics must absorb. A stronger model removes most of it.

## Decisions

- ADR 0008a: faults are text only; two brains read the same files; guided plus scripted suites;
  how the agent finds its trial's world; handoff on a successful `create_emergency_ticket`.
- Personas use `en-GB` (the agent is English), not the spec example's `fr-FR`. Voice profiles
  come with WS11.
