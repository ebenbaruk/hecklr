# 0007d. Chat adapters forward the trial's world URL; gpt-6-luna is the default live model

Date: 2026-10-21 · Status: proposed · Workstream: WS7d

## Context

- **World URL.** Every trial gets its own world sandbox under `/t/<trial id>` on a server that
  listens on a random port per run (ADR 0003a). `SessionContext` carries the URLs
  (`world_http_url`, `world_mcp_url`), but the chat adapters sent only the trial id. An HTTP agent
  could not find its trial's world, so the reference agent fell back to a private local copy:
  over `hecklr run` with the `http` adapter the clean agent failed all 15 booking, cancel and
  reschedule state-assertion scenarios of the smoke suite (passed 18/33), although it passes them
  in process. ADR 0008a's workaround, `ACME_WORLD_BASE_URL`, needs a fixed sandbox port that
  `hecklr.yaml` cannot set.
- **Model.** The project's live model is now OpenAI `gpt-6-luna` ($0.10 input, $0.01 cached
  input, $0.50 output per million tokens; user decision 2026-10-21). Ollama stays an optional
  free fallback. A live probe of `gpt-6-luna` and `gpt-5.6-luna` over Chat Completions
  (2026-10-21) found: `max_tokens` is rejected (`max_completion_tokens` works); any `temperature`
  other than the default 1 is rejected while the model reasons, and accepted with
  `reasoning_effort: none`; `seed` and JSON Schema `response_format` work; function tools need
  `reasoning_effort: none`; `minimal` effort is not supported.

## Decision

- **Every chat request names the trial's world.** `hecklr_adapters.session_headers(ctx)` adds
  `X-Hecklr-World-Url` (the OpenAPI base; tools at `POST <url>/tools/<name>`) and
  `X-Hecklr-World-Mcp-Url` to `X-Hecklr-Trial-Id` when the trial has a sandbox. The `http`,
  `openai-compatible` and `websocket` adapters send them. Request templates may also use
  `{{world_url}}` and `{{world_mcp_url}}`; they render empty in observed tool mode. Headers need
  no template change, so the default `http` config just works; placeholders serve agents that
  read the body only. No schema change: `SessionContext` already had the URLs.
- **The reference agent resolves its world per request**: body `world_url`, then the header,
  then `ACME_WORLD_BASE_URL`, then a local copy. `/healthz` reports `requests_by_world_source`, so
  a run that silently fell back to the local copy is visible.
- **Luna is the default** for the reference agent's LLM brain (`ACME_MODEL`) and the examples'
  simulator and judge. Both luna models are priced in `hecklr_llm.PRICES`; cached prompt tokens
  (`prompt_tokens_details.cached_tokens`) bill at `CACHED_INPUT_PER_MTOK`, since `ModelPrice` has
  no cached rate and the schema is frozen. A model without a cached rate bills them in full,
  which can only overstate spend.
- **A temperature means "no reasoning" on luna.** For models in
  `SAMPLING_ONLY_WITHOUT_REASONING`, the OpenAI provider sends `reasoning_effort: none` when the
  request sets a temperature (judges run at 0, and the spec wants repeatable judges). A request
  without a temperature keeps the model's default reasoning. `LLMRequest` has no reasoning field,
  so the temperature is the only signal the provider has.
- **The reference agent leaves the temperature to the model** (`ACME_TEMPERATURE` unset). Measured
  on the gas-smell opening: at temperature 0 (reasoning off) luna opened the emergency ticket in
  2 of 3 seeds even with a stricter step protocol and sometimes claimed to be "alerting the
  engineer" without the tool call; with default reasoning it did so in 5 of 5, at about 8 s and
  $0.0002 a turn. The step protocol now also says that a reply ends the turn.

## Consequences

- `hecklr run` over HTTP grades the world the agent acted on: the clean rules-brain agent passes
  33/33 smoke scenarios over the `http` adapter, and F2 fails exactly its 6 reschedule
  scenarios.
- Agents written for Hecklr should read `X-Hecklr-World-Url`; `ACME_WORLD_BASE_URL` remains for
  agents that cannot.
- The judge and an agent with a temperature run luna without reasoning: cheaper and repeatable,
  possibly less careful. Calibration (`hecklr calibrate --judges`) is where that shows; a
  reasoning field on `LLMRequest` would need an RFC.
- Live tests default to luna and need `OPENAI_API_KEY` plus `HECKLR_TEST_BUDGET_USD`; the
  four reference-agent flows cost about $0.0015 together. `ACME_LIVE_MODEL=ollama/...` and
  `HECKLR_LIVE_OLLAMA_MODEL` keep the free path. Supersedes ADR 0008a's default agent model and
  its "world URL" consequence.
