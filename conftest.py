"""Workspace-wide pytest hooks."""

# Imported up front, before any test runs: httpx2 imports httpcore2 lazily and caches a map
# from httpcore2's exception classes to its own. `pytester.runpytest_inprocess` (the hecklr
# pytest-plugin tests) restores sys.modules after its inner run, so a httpcore2 first imported
# there is dropped and re-imported later with new classes the cached map no longer matches.
# Transport errors then escape as raw httpcore2 exceptions, past every `except httpx2.HTTPError`,
# in whichever xdist worker ran those tests first.
import httpcore2  # noqa: F401 -- imported for the side effect above
import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Re-enable sockets for ``live`` tests; every other test runs with the network blocked."""
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(pytest.mark.enable_socket)
