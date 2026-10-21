"""Parsing of ``provider/model`` strings."""

from hecklr_llm.errors import UnknownProviderError


def split_model(model: str) -> tuple[str, str]:
    """Split ``"anthropic/claude-sonnet-5-5"`` into ``("anthropic", "claude-sonnet-5-5")``.

    Only the first slash separates the provider, so model names may contain slashes
    (``openai-compatible/meta-llama/Llama-3.1-8B``).
    """
    provider, sep, name = model.partition("/")
    if not sep or not provider or not name:
        raise UnknownProviderError(f"model {model!r} is not in 'provider/model' form")
    return provider, name
