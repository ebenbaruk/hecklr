"""Executed docs snippets: what the docs tell users to write or type must be accepted by Hecklr.

- Every YAML block titled with a project file (``hecklr.yaml``, ``scenarios/...``,
  ``personas/...``, ``worlds/...``) validates against the model Hecklr loads it with.
- The CLI and configuration reference pages are what ``docs/scripts/gen_reference.py``
  generates from the code today.
- Every ``hecklr`` command line in a bash block names a real command with real options: it is
  parsed by the CLI itself (with ``--help`` appended, so nothing runs).
"""

import shlex
import subprocess
import sys

import pytest
import yaml
from docs_snippets import REPO, CodeBlock, code_blocks, commands, site_pages
from pydantic import BaseModel
from typer.testing import CliRunner

from hecklr.cli import app
from hecklr.config import ProjectConfig
from hecklr_schemas import Persona, Scenario, World

_MODELS: dict[str, type[BaseModel]] = {
    "hecklr.yaml": ProjectConfig,
    "scenarios/": Scenario,
    "personas/": Persona,
    "worlds/": World,
}
_SHELLS = ("bash", "sh", "console", "shell")


def _yaml_blocks() -> list[CodeBlock]:
    return [
        block
        for page in site_pages()
        for block in code_blocks(page)
        if block.language == "yaml" and block.title is not None and _model_for(block.title)
    ]


def _model_for(title: str) -> type[BaseModel] | None:
    name = title.rsplit("/", 1)[-1] if title.endswith("hecklr.yaml") else title
    return next((model for prefix, model in _MODELS.items() if name.startswith(prefix)), None)


def _hecklr_lines() -> list[tuple[CodeBlock, list[str]]]:
    found = []
    for page in site_pages():
        for block in code_blocks(page):
            if block.language not in _SHELLS:
                continue
            for command in commands(block):
                for part in command.split("&&"):
                    argv = _hecklr_args(part)
                    if argv is not None:
                        found.append((block, argv))
    return found


def _hecklr_args(command: str) -> list[str] | None:
    """The arguments after ``hecklr`` in ``command`` (``uv run [opts] hecklr ...`` too)."""
    words = shlex.split(command.split("|", 1)[0])
    if "hecklr" not in words:
        return None
    start = words.index("hecklr")
    prefix = words[:start]
    if prefix and prefix[:2] != ["uv", "run"] and not all("=" in word for word in prefix):
        return None  # e.g. `pip install hecklr`
    # Placeholders such as <run id> stand for any value.
    args = words[start + 1 :]
    return ["x" if word.startswith("<") and word.endswith(">") else word for word in args]


def _ids(blocks: list[CodeBlock]) -> list[str]:
    return [f"{block.page.relative_to(REPO)}:{block.title}" for block in blocks]


@pytest.mark.parametrize("block", _yaml_blocks(), ids=_ids(_yaml_blocks()))
def test_documented_project_files_validate(block: CodeBlock) -> None:
    model = _model_for(block.title or "")
    assert model is not None
    model.model_validate(yaml.safe_load(block.body))


def test_docs_show_project_files_for_every_kind() -> None:
    kinds = {_model_for(block.title or "") for block in _yaml_blocks()}
    assert kinds == set(_MODELS.values())


def test_documented_hecklr_commands_parse() -> None:
    lines = _hecklr_lines()
    assert len(lines) > 30  # the guides show the CLI throughout
    runner = CliRunner()
    failures = []
    for block, argv in lines:
        result = runner.invoke(app, [*argv, "--help"])
        if result.exit_code != 0:
            failures.append(f"{block.where()}: hecklr {' '.join(argv)}\n{result.output}")
    assert not failures, "\n".join(failures)


def test_generated_reference_pages_match_the_code() -> None:
    generator = REPO / "docs" / "scripts" / "gen_reference.py"
    done = subprocess.run(  # noqa: S603 -- our own script with our own interpreter
        [sys.executable, str(generator), "--check"], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stderr
