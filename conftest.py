"""Workspace-wide pytest hooks."""

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Re-enable sockets for ``live`` tests; every other test runs with the network blocked."""
    for item in items:
        if item.get_closest_marker("live"):
            item.add_marker(pytest.mark.enable_socket)
