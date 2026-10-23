"""Workspace-wide pytest hooks."""

import os

# Imported up front, before any test runs: httpx2 imports httpcore2 lazily and caches a map
# from httpcore2's exception classes to its own. `pytester.runpytest_inprocess` (the hecklr
# pytest-plugin tests) restores sys.modules after its inner run, so a httpcore2 first imported
# there is dropped and re-imported later with new classes the cached map no longer matches.
# Transport errors then escape as raw httpcore2 exceptions, past every `except httpx2.HTTPError`,
# in whichever xdist worker ran those tests first.
import httpcore2  # noqa: F401 -- imported for the side effect above
import pytest

SC4_SERIAL = pytest.mark.skip(
    reason="SC4 timing needs an idle CPU (ADR 0010b): uv run pytest -m sc4_timing -p no:xdist"
)
PERF_SERIAL = pytest.mark.skip(
    reason="benchmarks need the whole machine (ADR 00hcb): uv run pytest -m perf -p no:xdist"
)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Re-enable sockets for ``live`` tests; every other test runs with the network blocked.

    ``sc4_timing`` tests measure the machine as much as Hecklr when other xdist workers share
    the CPU, so a parallel run skips them; CI asserts them in a serial step (ADR 0010b). So do
    ``perf`` benchmarks, which measure throughput with every core to themselves (ADR 00hcb).
    """
    parallel = bool(os.environ.get("PYTEST_XDIST_WORKER"))
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(pytest.mark.enable_socket)
        if parallel and item.get_closest_marker("sc4_timing"):
            item.add_marker(SC4_SERIAL)
        if parallel and item.get_closest_marker("perf"):
            item.add_marker(PERF_SERIAL)
