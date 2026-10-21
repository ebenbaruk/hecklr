# hecklr-llm

The LLM provider layer for [Hecklr](../../README.md): Anthropic, OpenAI and any OpenAI-compatible
endpoint (Ollama, vLLM), a deterministic `FakeLLM` for tests, and an on-disk response cache so an
unchanged call is never paid for twice. Owned by WS0; see `docs/adr/0003-llm-provider-layer-and-cache.md`.

```python
from hecklr_llm import CachedProvider, LLMRequest, Message, ProviderRouter, complete_structured

async with ProviderRouter() as router:
    llm = CachedProvider(router)
    reply = await llm.complete(
        LLMRequest(model="ollama/qwen3:8b", messages=[Message(role="user", content="Hi")])
    )
```

Model strings are `provider/model`: `anthropic/…`, `openai/…`, `ollama/…`,
`openai-compatible/…` (base URL from `hecklr.yaml` or `OPENAI_COMPATIBLE_BASE_URL`), `fake/…`.

Paid models need a known price: models missing from the built-in table go under `models.prices`
in `hecklr.yaml`, otherwise calls are refused with `UnknownPriceError` rather than counted as free.

`FakeLLM` picks replies from `rules` (regex on the last message), a `responder` function, or a
`script`. Rules and responders depend only on the request, so they stay deterministic when calls
run concurrently. A script returns replies in call order, so under concurrency (for example three
judges running at once) which call gets which reply depends on scheduling. Use rules or a
responder whenever calls can overlap.
