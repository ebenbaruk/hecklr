"""Fixtures: starter projects written by ``hecklr init``'s scaffold, run on scripted models."""

import importlib.util
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from hecklr.scaffold import STARTER_AGENT_MODULE, AgentChoice, ModelChoice, write_project

FAKE_MODELS = ModelChoice("fake/customer", "fake/judge", "tests")


@pytest.fixture(autouse=True)
def isolated_imports(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test imports its own project's starter agent and leaves ``sys.path`` as it was."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, STARTER_AGENT_MODULE, raising=False)


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A starter project with the in-process agent and ``fake/`` models."""
    root = tmp_path / "shop"
    write_project(root, "shop-agent", AgentChoice("python"), FAKE_MODELS)
    return root


@pytest.fixture
def config_path(project_dir: Path) -> Path:
    """The starter project's ``hecklr.yaml``."""
    return project_dir / "hecklr.yaml"


@pytest.fixture
def edit() -> Callable[[Path, str, str], None]:
    """Replace a snippet (which must be present) in a project file."""

    def replace(path: Path, old: str, new: str) -> None:
        text = path.read_text(encoding="utf-8")
        assert old in text, f"{old!r} not in {path}"
        path.write_text(text.replace(old, new), encoding="utf-8")

    return replace


@pytest.fixture
def http_agent_url() -> Iterator[str]:
    """The chat URL of ``fixtures/http_agent.py``, served on an ephemeral 127.0.0.1 port."""
    path = Path(__file__).parent / "fixtures" / "http_agent.py"
    spec = importlib.util.spec_from_file_location("hecklr_fixture_http_agent", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with module.serve_in_thread() as url:
        yield url
