from pathlib import Path

import pytest

from hecklr_fix.editable import load_editable, normalize
from hecklr_fix.errors import FixError
from hecklr_schemas import HecklrConfig


def _config(editable: list[str]) -> HecklrConfig:
    return HecklrConfig.model_validate(
        {
            "version": 1,
            "agent": {
                "name": "acme",
                "adapters": {"chat": {"type": "http", "url": "http://127.0.0.1:8000/chat"}},
                "editable": editable,
            },
            "world": "worlds/acme.yaml",
            "suites": {"ci": {"scenarios": "scenarios/**/*.yaml"}},
            "models": {"simulator": "fake/sim", "judge": "fake/judge"},
        }
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    for relative, text in {
        "hecklr.yaml": "version: 1\n",
        "worlds/acme.yaml": "id: acme\n",
        "scenarios/core/a.yaml": "id: a\n",
        "personas/p.yaml": "id: p\n",
        "prompts/system.md": "Be kind.\n",
    }.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def test_editable_files_are_read_by_their_normalized_path(project: Path) -> None:
    files = load_editable(_config(["./prompts/system.md"]), project / "hecklr.yaml")

    assert files.root == project.resolve()
    assert files.contents == {"prompts/system.md": "Be kind.\n"}


@pytest.mark.parametrize(
    "protected",
    ["hecklr.yaml", "worlds/acme.yaml", "scenarios/core/a.yaml", "personas/p.yaml"],
)
def test_guardrail_files_that_define_the_tests_are_never_editable(
    project: Path, protected: str
) -> None:
    with pytest.raises(FixError, match="defines the tests"):
        load_editable(_config(["prompts/system.md", protected]), project / "hecklr.yaml")


@pytest.mark.parametrize("entry", ["/etc/passwd", "../outside.md", ""])
def test_editable_paths_must_stay_inside_the_project(project: Path, entry: str) -> None:
    with pytest.raises(FixError, match="relative path inside the project"):
        load_editable(_config([entry]), project / "hecklr.yaml")


def test_a_symlink_out_of_the_project_is_refused(
    project: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside") / "secret.md"
    outside.write_text("x", encoding="utf-8")
    (project / "prompts" / "link.md").symlink_to(outside)

    with pytest.raises(FixError, match="outside the project"):
        load_editable(_config(["prompts/link.md"]), project / "hecklr.yaml")


def test_missing_and_empty_editable_lists_fail_with_a_hint(project: Path) -> None:
    with pytest.raises(FixError, match=r"declares no agent\.editable"):
        load_editable(_config([]), project / "hecklr.yaml")
    with pytest.raises(FixError, match=r"cannot read editable file 'prompts/none\.md'"):
        load_editable(_config(["prompts/none.md"]), project / "hecklr.yaml")


def test_windows_separators_are_normalized() -> None:
    assert normalize("prompts\\system.md") == "prompts/system.md"
