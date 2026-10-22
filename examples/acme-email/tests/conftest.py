"""Fixtures for the acme-email tests.

Neither agent is an installed package: like ``agent/serve.py``, the tests put both agents'
directories (and this one, for ``email_testing``) on ``sys.path``.
"""

import sys
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
EXAMPLES = TESTS_DIR.parents[1]
for directory in (EXAMPLES / "acme-maintenance" / "agent", TESTS_DIR.parent / "agent", TESTS_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

# Importable only once the directories above are on sys.path.
from acme_agent import PROJECT_ROOT, WORLD_FILE  # noqa: E402
from email_testing import persona_library  # noqa: E402

from hecklr_schemas import Persona, World  # noqa: E402
from hecklr_world import load_world  # noqa: E402


@pytest.fixture(scope="session")
def world() -> World:
    return load_world(PROJECT_ROOT / WORLD_FILE)


@pytest.fixture(scope="session")
def personas() -> dict[str, Persona]:
    return persona_library()
