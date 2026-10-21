"""Content addresses for requests: the cache key and FakeLLM's source of determinism."""

import hashlib
import json

from hecklr_schemas import LLMRequest

KEY_VERSION = "hecklr-llm-request-v1"
"""Bump to invalidate every cached response when the key derivation changes."""


def request_key(request: LLMRequest) -> str:
    """SHA-256 over the canonical JSON of every field that can change the answer.

    Keys are sorted at every level so semantically equal requests (e.g. JSON schemas written with
    keys in a different order) share one key.
    """
    canonical = json.dumps(
        [KEY_VERSION, request.model_dump(mode="json")],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
