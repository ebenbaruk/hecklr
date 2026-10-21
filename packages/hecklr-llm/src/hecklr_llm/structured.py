"""Structured (JSON) output on top of any ``LLMProvider``."""

import json
import re

from pydantic import BaseModel, JsonValue, ValidationError

from hecklr_llm.errors import LLMOutputError
from hecklr_schemas import LLMProvider, LLMRequest, LLMResponse


def decode_json_reply(text: str) -> JsonValue:
    """Decode a reply that was requested as JSON, failing loudly if it is not."""
    try:
        value: JsonValue = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"model reply is not valid JSON ({exc.msg}): {text[:200]!r}") from exc
    return value


async def complete_structured[T: BaseModel](
    provider: LLMProvider, request: LLMRequest, response_type: type[T]
) -> tuple[T, LLMResponse]:
    """Ask for a reply matching ``response_type`` and return it validated, with the raw response.

    The request's ``json_schema`` is replaced by the model's JSON Schema, so callers write prompts
    and Pydantic models, never schemas.
    """
    structured_request = LLMRequest.model_validate(
        {
            **request.model_dump(),
            "json_schema": response_type.model_json_schema(),
            "schema_name": re.sub(r"[^A-Za-z0-9_-]", "_", response_type.__name__)[:64],
        }
    )
    response = await provider.complete(structured_request)
    if response.truncated:
        raise LLMOutputError(
            f"reply for {response_type.__name__} was cut off at max_tokens="
            f"{request.max_tokens}; raise max_tokens"
        )
    try:
        value = response_type.model_validate_json(response.text)
    except ValidationError as exc:
        raise LLMOutputError(
            f"model reply does not match {response_type.__name__}: {exc.error_count()} error(s); "
            f"reply was {response.text[:200]!r}"
        ) from exc
    return value, response
