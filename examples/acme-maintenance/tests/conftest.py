"""Fixtures for the acme-maintenance tests.

The agent is not an installed package: like ``agent/serve.py``, the tests put ``agent/`` (and
this directory, for ``acme_testing``) on ``sys.path``.
"""

import sys
from collections.abc import Callable
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
for directory in (TESTS_DIR.parent / "agent", TESTS_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

# Importable only once the directories above are on sys.path.
from acme_agent import PROJECT_ROOT, WORLD_FILE  # noqa: E402
from acme_testing import Chat, persona_library, rules_agent  # noqa: E402

from hecklr_schemas import Persona, World  # noqa: E402
from hecklr_world import load_world  # noqa: E402


@pytest.fixture(scope="session")
def world() -> World:
    return load_world(PROJECT_ROOT / WORLD_FILE)


@pytest.fixture(scope="session")
def personas() -> dict[str, Persona]:
    return persona_library()


@pytest.fixture
def chat(world: World) -> Callable[..., Chat]:
    """``chat(variant="clean")``: a fresh conversation with the rules agent."""

    def make(variant: str = "clean") -> Chat:
        return Chat(rules_agent(variant), world)

    return make
