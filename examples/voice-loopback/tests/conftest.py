"""Fixtures for the voice-loopback tests.

The agent is not an installed package: like ``serve.py``, the tests put this example's directory
on ``sys.path``.
"""

import gc
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

EXAMPLE_DIR = Path(__file__).resolve().parents[1]
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))


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
