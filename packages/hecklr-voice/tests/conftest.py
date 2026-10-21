"""Voice test configuration."""

import gc
from collections.abc import Iterator

import pytest


@pytest.fixture
def frozen_heap() -> Iterator[None]:
    """Move the test session's objects out of the collector's way, as a long-lived harness should.

    A full collection over pytest's heap pauses the loop for tens of milliseconds: a property of
    the test process, not of the engine (ADR 0009a), and noise in loop-timing measurements.
    """
    gc.collect()
    gc.freeze()
    yield
    gc.unfreeze()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Run the SC3 concurrency benchmark only when asked for (``-m perf``).

    It measures real-time frame pacing for 50 calls, which is only meaningful on a machine that
    is not also running the rest of the suite in parallel.
    """
    if "perf" in (config.getoption("markexpr") or ""):
        return
    skip = pytest.mark.skip(reason="benchmark: run alone with -m perf (SC3)")
    for item in items:
        if item.get_closest_marker("perf") and "hecklr-voice" in str(item.path):
            item.add_marker(skip)
