# 0008a. Reference agent: two brains, file-only faults, and how it reaches its trial's world

Date: 2026-10-21 · Status: proposed · Workstream: WS8

> Amended by ADR 0007d: Hecklr's chat adapters now send the trial's world URL
> (`X-Hecklr-World-Url`, `{{world_url}}`), which the agent prefers to `ACME_WORLD_BASE_URL`, and the
> default agent model is `openai/gpt-6-luna` (Ollama is the free override).

## Context

SC8 needs a reference agent (`acme-maintenance`) and seeded faults that Hecklr must catch, with
the clean agent passing an A/A comparison. F1-F6 are behavioural: a missing instruction, a wrong
date format, an invented fee, a data leak, an unverified refund, a missing handoff. Running the
matrix with a real model costs time (a local 8B model) or money, and an LLM agent is noisy by
nature. The agent must also call its own trial's world sandbox, but the `http` chat adapter
sends only `{{message}}`, `{{session_id}}`, `{{trial_id}}` and the `X-Hecklr-Trial-Id` header,
not `SessionContext.world_http_url`.

## Decision

- **A fault is only a text change.** Each `examples/acme-faults/F<n>/` is an overlay holding one
  editable file (`prompts/system.md` or `tools/descriptions.yaml`) with exactly one line changed,
  and a README. The loader refuses an overlay with any other file. A test checks the one-line
  property for every variant.
- **Two brains read the same files.** The LLM brain (`hecklr_llm`, structured `AgentStep`
  answers, since `LLMRequest` has no native tool calls) is the real reference agent. The rules
  brain is a deterministic CI stand-in: keyword understanding, one task at a time, the same tool
  calls. It reads the files through `Behaviour`: each rule a fault removes or rewrites is a
  pattern in the prompt, and the slot format is parsed from the tool description. So F1-F6 change
  it exactly as they change what the LLM is told.
- **Two scenario sets.** `scenarios/` (64 guided scenarios, the ADR 0005a reference suite) runs
  with the LLM simulator and judges. `scripted/` (33 scenarios, `smoke` suite) has scripted
  customers and deterministic graders only. With the rules brain, the smoke suite is a free,
  exactly repeatable fault matrix: the clean agent passes all 33 trials, and each fault fails
  exactly the scenarios tagged `catches-f<n>` (at least 6 each, so a fully flipped fault has a
  paired sign-flip p of at most 2 / 2^6 < 0.05). Every guided fault area has at least 6
  scenarios, most tagged `core`, for the same reason in the `ci` suite.
- **The world URL.** The agent resolves its tools per conversation: a `world_url` in the request
  body if present, else `ACME_WORLD_BASE_URL` + `/t/<trial id from X-Hecklr-Trial-Id>` (the
  sandbox's routing, ADR 0003a), else a private in-process copy of the world. With the sandbox,
  the sandbox logs tool calls, so the agent does not stream them (ADR 0006a). With the local
  copy, nobody else sees them, so it streams `tool_call` / `tool_result` events.
- **Handoff is infrastructure.** A successful `create_emergency_ticket` ends the agent's part of
  the conversation with a `handoff` and an `end` event, whichever brain called it.
- **No Python tools in the world**, so `hecklr run` never needs the example on `sys.path`.
- **Default agent model: `ollama/mistral:latest`.** Measured locally: `qwen3:8b` reasons before
  every step (40-250 s a turn, and it exhausts the 1024-token answer budget), and
  `qwen3:4b-instruct` ignores fault prompts (it still opens an emergency ticket under F6).
  Mistral follows both the clean and the faulty prompts. A reference agent that ignores its
  prompt cannot show prompt faults. The catalog is rendered as prose, not JSON Schema, to fit
  Ollama's default 4k context.

## Consequences

- `ACME_WORLD_BASE_URL` needs the run's sandbox on a known port. Today `Sandbox(port=...)` takes
  one but `hecklr.yaml` cannot set it. The composition (WS7a) should either pin the port or,
  simpler for every agent, let the `http` adapter send the world URL (a `{{world_http_url}}`
  template placeholder or a header). That needs no schema change: `SessionContext` already
  carries the URL. The agent already reads a body `world_url`.
- The rules brain is not a good agent, and must not be read as one. It understands simulated
  customers only as far as its keywords reach, so its guided-suite pass rate says little. Its job
  is the deterministic matrix and plumbing tests.
- Rewording a prompt rule without its keywords changes the rules brain's behaviour but maybe not
  the LLM's. The patterns are documented in `rules/behaviour.py`, and the fixer (WS15) is scored on
  the LLM brain.
