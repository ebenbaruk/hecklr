from decimal import Decimal

import pytest

from hecklr_llm import (
    AnthropicProvider,
    FakeLLM,
    OpenAIProvider,
    ProviderConfigError,
    ProviderRouter,
    UnknownProviderError,
    split_model,
)
from hecklr_llm.router import DEFAULT_OLLAMA_BASE_URL
from hecklr_schemas import LLMRequest, Message, ModelPrice, ProviderSettings


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("anthropic/claude-sonnet-5-5", ("anthropic", "claude-sonnet-5-5")),
        ("ollama/qwen3:8b", ("ollama", "qwen3:8b")),
        (
            "openai-compatible/meta-llama/Llama-3.1-8B",
            ("openai-compatible", "meta-llama/Llama-3.1-8B"),
        ),
    ],
)
def test_split_model_uses_the_first_slash(model: str, expected: tuple[str, str]) -> None:
    assert split_model(model) == expected


@pytest.mark.parametrize("model", ["claude", "/x", "anthropic/"])
def test_split_model_rejects_malformed_strings(model: str) -> None:
    with pytest.raises(UnknownProviderError, match="provider/model"):
        split_model(model)


@pytest.mark.anyio
async def test_router_dispatches_on_the_prefix() -> None:
    sim = FakeLLM(script=["from sim"])
    router = ProviderRouter(providers={"fake": sim})

    reply = await router.complete(
        LLMRequest(model="fake/sim", messages=[Message(role="user", content="hi")])
    )

    assert reply.text == "from sim"
    assert router.provider_for("fake/judge") is sim


def test_providers_are_created_once_and_lazily() -> None:
    router = ProviderRouter(env={"ANTHROPIC_API_KEY": "test-key"})

    first = router.provider_for("anthropic/claude-sonnet-5-5")

    assert isinstance(first, AnthropicProvider)
    assert router.provider_for("anthropic/claude-haiku-5-5") is first


def test_openai_and_ollama_use_the_openai_provider() -> None:
    router = ProviderRouter(env={"OPENAI_API_KEY": "test-key"})

    assert isinstance(router.provider_for("openai/gpt-4.1"), OpenAIProvider)
    assert isinstance(router.provider_for("ollama/qwen3:8b"), OpenAIProvider)
    assert isinstance(router.provider_for("fake/x"), FakeLLM)


@pytest.mark.parametrize(
    ("model", "env_var"),
    [("anthropic/claude-sonnet-5-5", "ANTHROPIC_API_KEY"), ("openai/gpt-4.1", "OPENAI_API_KEY")],
)
def test_missing_api_key_is_a_config_error_naming_the_variable(model: str, env_var: str) -> None:
    with pytest.raises(ProviderConfigError, match=env_var):
        ProviderRouter(env={}).provider_for(model)


def test_api_key_variable_can_be_renamed_in_settings() -> None:
    settings = {"anthropic": ProviderSettings(api_key_env="ACME_CLAUDE_KEY")}

    with pytest.raises(ProviderConfigError, match="ACME_CLAUDE_KEY"):
        ProviderRouter(settings, env={"ANTHROPIC_API_KEY": "unused"}).provider_for(
            "anthropic/claude-sonnet-5-5"
        )
    router = ProviderRouter(settings, env={"ACME_CLAUDE_KEY": "test-key"})
    assert isinstance(router.provider_for("anthropic/claude-sonnet-5-5"), AnthropicProvider)


def test_configured_prices_reach_the_providers() -> None:
    price = ModelPrice(input_per_mtok=Decimal(0), output_per_mtok=Decimal(0))
    router = ProviderRouter(
        prices={"openai-compatible/llama-70b": price},
        env={"OPENAI_COMPATIBLE_BASE_URL": "http://vllm:8000/v1"},
    )

    provider = router.provider_for("openai-compatible/llama-70b")

    assert isinstance(provider, OpenAIProvider)
    assert provider._prices["openai-compatible/llama-70b"] == price
    assert "anthropic/claude-sonnet-5-5" in provider._prices


def test_ollama_defaults_to_localhost() -> None:
    router = ProviderRouter(env={})

    assert router._url("ollama") is None
    assert DEFAULT_OLLAMA_BASE_URL == "http://localhost:11434/v1"
    assert isinstance(router.provider_for("ollama/qwen3:8b"), OpenAIProvider)


def test_base_url_comes_from_settings_before_environment() -> None:
    router = ProviderRouter(
        {
            "openai-compatible": ProviderSettings.model_validate(
                {"base_url": "http://gpu:8000/v1", "api_key_env": "GPU_KEY"}
            )
        },
        env={"OPENAI_COMPATIBLE_BASE_URL": "http://ignored:1/v1", "GPU_KEY": "k"},
    )

    assert router._url("openai-compatible") == "http://gpu:8000/v1"
    assert router._api_key("openai-compatible") == "k"
    assert isinstance(router.provider_for("openai-compatible/llama"), OpenAIProvider)


def test_base_url_falls_back_to_environment() -> None:
    router = ProviderRouter(env={"OPENAI_COMPATIBLE_BASE_URL": "http://vllm:8000/v1"})

    assert router._url("openai-compatible") == "http://vllm:8000/v1"
    assert router._api_key("openai-compatible") is None


def test_openai_compatible_without_a_base_url_is_a_config_error() -> None:
    router = ProviderRouter(env={})

    with pytest.raises(ProviderConfigError, match="OPENAI_COMPATIBLE_BASE_URL"):
        router.provider_for("openai-compatible/llama")


def test_unknown_provider_lists_the_known_ones() -> None:
    with pytest.raises(UnknownProviderError, match="anthropic, openai, ollama"):
        ProviderRouter(env={}).provider_for("mistral/large")


@pytest.mark.anyio
async def test_router_closes_only_the_clients_it_created() -> None:
    injected = FakeLLM()
    env = {"OPENAI_API_KEY": "k", "ANTHROPIC_API_KEY": "k"}
    async with ProviderRouter(providers={"fake": injected}, env=env) as router:
        created_openai = router.provider_for("openai/gpt-4.1")
        created_anthropic = router.provider_for("anthropic/claude-sonnet-5-5")
        assert router.provider_for("fake/x") is injected

    assert isinstance(created_openai, OpenAIProvider)
    assert isinstance(created_anthropic, AnthropicProvider)
    assert created_openai._client.is_closed()
    assert created_anthropic._client.is_closed()
    assert router._owned == []
