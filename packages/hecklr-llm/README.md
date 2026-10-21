# hecklr-llm

The LLM provider layer for [Hecklr](../../README.md): Anthropic, OpenAI and any OpenAI-compatible
endpoint (Ollama, vLLM), a deterministic `FakeLLM` for tests, and an on-disk response cache so an
unchanged call is never paid for twice. Owned by WS0; see `docs/adr/0003-llm-provider-layer-and-cache.md`.

```python
from hecklr_llm import CachedProvider, LLMRequest, Message, ProviderRouter, complete_structured

async with ProviderRouter() as router:
    llm = CachedProvider(router)
    reply = await llm.complete(
        LLMRequest(model="openai/gpt-6-luna", messages=[Message(role="user", content="Hi")])
    )
```

Model strings are `provider/model`: `anthropic/…`, `openai/…`, `ollama/…`,
`openai-compatible/…` (base URL from `hecklr.yaml` or `OPENAI_COMPATIBLE_BASE_URL`), `fake/…`.
Hecklr's default live model is `openai/gpt-6-luna` (ADR 0007d); a local `ollama/…` model is
the free alternative.

Paid models need a known price: models missing from the built-in table go under `models.prices`
in `hecklr.yaml`, otherwise calls are refused with `UnknownPriceError` rather than counted as free.

| Model | Input $/1M | Cached input $/1M | Output $/1M |
| --- | --- | --- | --- |
| `openai/gpt-6-luna` | 0.10 | 0.01 | 0.50 |
| `openai/gpt-5.6-luna` | 0.20 | 0.02 | 1.20 |

Prompt tokens the API reports as cached (`prompt_tokens_details.cached_tokens`) bill at the
cached rate (`CACHED_INPUT_PER_MTOK`); other models bill them at the full input price.

The luna models are reasoning models: they need `max_completion_tokens` (the default for
`openai/`), accept JSON Schema output, and reject any temperature but 1 while reasoning. A
request that sets `temperature` (judges run at 0) is therefore sent with `reasoning_effort:
none`; a request without one keeps the model's default reasoning.

Live tests: `uv run pytest packages/hecklr-llm -m live` runs `test_live_openai.py` (needs
`OPENAI_API_KEY` and `HECKLR_TEST_BUDGET_USD`, a fraction of a cent) and `test_live_ollama.py`
(skipped unless Ollama is running).

`FakeLLM` picks replies from `rules` (regex on the last message), a `responder` function, or a
`script`. Rules and responders depend only on the request, so they stay deterministic when calls
run concurrently. A script returns replies in call order, so under concurrency (for example three
judges running at once) which call gets which reply depends on scheduling. Use rules or a
responder whenever calls can overlap.
