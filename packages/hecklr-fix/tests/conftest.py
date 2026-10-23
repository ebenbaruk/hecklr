from pathlib import Path

import pytest

from .factories import write_project


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A tiny agent project; returns its ``hecklr.yaml``."""
    return write_project(tmp_path / "project")
