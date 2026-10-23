"""Voice test configuration."""

import gc
import os
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
    """Run the voice timing tests only when asked for, and only serially (#55).

    SC3's frame pacing for 50 calls, the loop's CPU between heartbeats and Whisper's CPU per
    caller turn are only meaningful on a machine that is not also running the rest of the suite:
    like SC4 (ADR 0010b), they are skipped inside an xdist worker and asserted by their own
    serial step, ``uv run pytest packages/hecklr-voice -m perf -p no:xdist``.
    """
    if "perf" not in (config.getoption("markexpr") or ""):
        reason = "timing: run alone with -m perf -p no:xdist"
    elif os.environ.get("PYTEST_XDIST_WORKER"):
        reason = "timing needs an idle CPU: uv run pytest -m perf -p no:xdist"
    else:
        return
    for item in items:
        if item.get_closest_marker("perf") and "hecklr-voice" in str(item.path):
            item.add_marker(pytest.mark.skip(reason=reason))
