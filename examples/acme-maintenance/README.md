# acme-maintenance: Hecklr's reference agent (chat)

The support agent of Acme Maintenance, a fictional heating, plumbing and electrical company in
Paris. It books, moves and cancels visits, answers invoice and fee questions, issues refunds
after verifying the customer, and hands gas smells and other emergencies to a human at once.
Hecklr's fault-injection matrix (SC8) runs against it: `../acme-faults/` holds the seeded
faults F1-F6, each a one-line change to one of the agent's editable files.

```text
hecklr.yaml                 suites: ci (core, 3 trials), full (64 scenarios x 5), redteam, smoke
worlds/acme-maintenance.yaml   customers, visits, slots, invoices, refunds, policies, tickets
prompts/system.md           editable: the system prompt
tools/descriptions.yaml     editable: the tool catalog the agent's model sees
personas/                   8 customers + adversarial/ (refund fraudster, data prober)
scenarios/                  64 guided scenarios (LLM-simulated customers, judges)
scripted/                   33 scripted scenarios, deterministic graders only (smoke suite)
agent/acme_agent/           the agent (FastAPI service, brains, tool backends)
tests/                      world, suite, brains, faults, HTTP/SSE, live LLM
```

## Two brains

- **LLM brain** (default, `--brain llm`): the real reference agent. A tool-calling loop over
  `hecklr_llm` (`ACME_MODEL`, default `openai/gpt-6-luna`, needs `OPENAI_API_KEY`): each step
  the model answers one JSON object, either a tool call or the reply to the customer. For a free
  local run use `ACME_MODEL=ollama/mistral:latest ACME_TEMPERATURE=0`.
- **Rules brain** (`--brain rules`): a deterministic CI stand-in, **not** an example of agent
  design. It understands customers by keywords and runs one task at a time, calling the same
  tools. It reads the same `prompts/system.md` and `tools/descriptions.yaml`
  (`acme_agent/rules/behaviour.py`), so a fault variant changes it exactly as it changes what the
  LLM is told. With the scripted `smoke` suite it makes the fault matrix free and exactly
  repeatable.

## Running it

```bash
uv sync --all-packages --all-extras
uv run python examples/acme-maintenance/agent/serve.py --brain rules --port 8000          # clean
uv run python examples/acme-maintenance/agent/serve.py --brain rules --fault F3 --port 8000  # a fault
curl -s localhost:8000/healthz
curl -s localhost:8000/chat -H 'content-type: application/json' \
  -d '{"message": "I can smell gas in the kitchen", "session_id": "demo"}'
curl -sN localhost:8000/chat/stream -H 'content-type: application/json' \
  -d '{"message": "I want to cancel my visit", "session_id": "demo-2"}'
```

Every flag has an environment variable: `ACME_BRAIN`, `ACME_MODEL`, `ACME_FAULT`,
`ACME_OVERLAY`, `ACME_WORLD_BASE_URL`, `ACME_TODAY` (default 2026-10-21, the world's "today"),
`ACME_SEED`, `ACME_TEMPERATURE` (default: the model's own; on `gpt-6-luna` any value turns its
reasoning off, see ADR 0007d), `ACME_TOOL_TIMEOUT_S`.

### HTTP API

| Route | Request | Response |
| --- | --- | --- |
| `POST /chat` | `{"message", "session_id", "trial_id"?, "world_url"?}` | `{"reply", "handoff", "ended", "tool_calls"}` |
| `POST /chat/stream` | same | SSE: unnamed events are text deltas; `tool_call`, `tool_result`, `handoff`, `end` carry `hecklr_schemas` payloads |
| `GET /healthz` | | `{"status", "brain", "variant", "model", "fallback_tools", "requests_by_world_source"}` |

Both routes match Hecklr's `http` adapter (`hecklr.yaml` uses `/chat/stream` with `stream: sse`).
The conversation is keyed by `session_id`.

### Which world the tools act on

Per request, the first of:

1. `world_url` in the request body (a `{{world_url}}` request template placeholder);
2. the `X-Hecklr-World-Url` header: the trial's world sandbox (`.../t/<trial id>`). Hecklr's
   chat adapters send it with every request, so `hecklr run` needs no extra setup (ADR 0007d);
3. `ACME_WORLD_BASE_URL` + `/t/<trial id>`, the trial id read from the `X-Hecklr-Trial-Id`
   header (Hecklr's sandbox routes every trial under `/t/<trial id>`, ADR 0003a);
4. a private in-process copy of `worlds/acme-maintenance.yaml`, for manual runs. Hecklr cannot
   see these calls, so the agent then streams its `tool_call` / `tool_result` events itself.

`GET /healthz` counts the requests served from each source (`requests_by_world_source`): after
a `hecklr run`, only `header` should have grown. A growing `local` count means the agent graded
nothing Hecklr can see.

### In process

`acme_agent.InProcessAgent` is a `python`-adapter callable (`hecklr_adapters.PythonAgent`); its
tools go to `SessionContext.world_http_url` when the session has one:

```python
async with InProcessAgent(AcmeAgent(files, AgentSettings(brain="rules")), world) as chat:
    target = PythonChatTarget(chat)
```

## Tests

```bash
uv run pytest examples/acme-maintenance               # everything but the live test
uv run pytest examples/acme-maintenance -m live       # LLM brain on gpt-6-luna (~$0.002)
ACME_LIVE_MODEL=ollama/mistral:latest uv run pytest examples/acme-maintenance -m live  # free
```

The live tests need `OPENAI_API_KEY` and `HECKLR_TEST_BUDGET_USD` (e.g. `uv run --env-file .env
pytest ...`); they book a visit, refund an invoice after verification, hand a gas smell to a
human and check that the F4 overlay leaks an address the clean agent refuses.

`tests/test_suite.py` runs the 33 scripted scenarios as graded trials in process (scripted
customer, `python` adapter, chat driver, `TrialWorld`, `hecklr_grade`): the clean agent passes
all of them and each fault fails exactly the scenarios tagged `catches-f<n>`.
