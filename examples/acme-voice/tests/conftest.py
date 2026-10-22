"""Fixtures for the acme-voice tests.

Neither agent is an installed package: like ``agent/serve.py``, the tests put the voice agent,
the chat agent whose brain it reuses, and this directory (for ``voice_testing``) on ``sys.path``.
"""

import gc
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
EXAMPLES = TESTS_DIR.parents[1]
for directory in (EXAMPLES / "acme-maintenance" / "agent", TESTS_DIR.parent / "agent", TESTS_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))


@pytest.fixture
def frozen_heap() -> Iterator[None]:
    """Keep the test session's heap out of the collector's way, as a long-lived harness does.

    A full collection over pytest's heap pauses the event loop for tens of milliseconds: noise in
    a timing measurement that a real ``hecklr run`` avoids (ADR 0009a).
    """
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()
