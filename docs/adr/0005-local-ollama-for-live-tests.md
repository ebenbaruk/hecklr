# 0005. Local Ollama as the default target for live model tests

Date: 2026-10-21 · Status: accepted · Workstream: WS0

## Context

Unit tests never touch the network (they use `FakeLLM` or mocked HTTP), but provider code must
also be proven against a real model: request shapes, structured output and token accounting drift
with SDK and server versions. Cloud keys are not always available to contributors or CI, and real
calls cost money (`HECKLR_TEST_BUDGET_USD`).

## Decision

- Tests that call a real model are marked `@pytest.mark.live` and deselected by default
  (`addopts = -m "not live"`); run them with `uv run pytest -m live`.
- The default live target is a local Ollama through its OpenAI-compatible API
  (`ollama/qwen3:4b-instruct`, overridable with `HECKLR_LIVE_OLLAMA_MODEL` and `OLLAMA_BASE_URL`).
  It exercises the same `OpenAIProvider` code path used for vLLM and other compatible servers, and
  costs nothing.
- A live test skips itself, with a reason, when its server or model is unavailable, so `-m live`
  is safe to run anywhere.
- Live tests against paid APIs (Anthropic, OpenAI) are added by the workstreams that need them and
  must stay under `HECKLR_TEST_BUDGET_USD`.

## Consequences

- Contributors without cloud keys can still verify the provider layer end to end.
- A small local model is a weak judge of prompt quality; live tests assert on the plumbing
  (reachability, structure, usage), not on answer quality, which is the fault matrix's job.
