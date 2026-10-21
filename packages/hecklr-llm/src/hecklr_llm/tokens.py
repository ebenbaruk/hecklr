"""Deterministic token estimates for budgeting and for FakeLLM usage reports."""

import json
import math

from hecklr_schemas import LLMRequest

CHARS_PER_TOKEN = 4
"""Rough average for English text across current tokenizers; good enough for budget estimates."""


def estimate_tokens(text: str) -> int:
    """Estimate the token count of ``text`` (0 for empty text)."""
    return math.ceil(len(text) / CHARS_PER_TOKEN)


def estimate_input_tokens(request: LLMRequest) -> int:
    """Estimate the prompt size of ``request``: system, messages and output schema."""
    parts = [request.system or "", *(message.content for message in request.messages)]
    if request.json_schema is not None:
        parts.append(json.dumps(request.json_schema, separators=(",", ":")))
    return sum(estimate_tokens(part) for part in parts)
