"""Seed derivation: every random choice and LLM call in a trial gets its own stable seed.

Seeds are hashed from the trial seed plus labels (e.g. ``"turn", 3, attempt``) instead of drawn
from a shared generator, so a value never depends on how many draws happened before it. That keeps
cached reruns free: an unchanged turn always produces the same request, hence the same cache key.
"""

import hashlib
import json

SEED_MODULUS = 2**31 - 1
"""Derived seeds stay below this so every provider accepts them (some reject 64-bit seeds)."""


def derive_seed(seed: int, *labels: str | int) -> int:
    """Return a seed in ``[0, SEED_MODULUS)`` determined only by ``seed`` and ``labels``."""
    payload = json.dumps([seed, *labels]).encode()
    digest = hashlib.blake2b(payload, digest_size=8).digest()
    return int.from_bytes(digest, "big") % SEED_MODULUS


def unit_draw(seed: int, *labels: str | int) -> float:
    """Return a uniform draw in ``[0, 1)`` determined only by ``seed`` and ``labels``."""
    return derive_seed(seed, *labels) / SEED_MODULUS
