"""``--help`` output: hand-written ``[default: ...]`` notes survive Rich markup (issue #107)."""

import re

import click
import pytest
import typer
import typer.rich_utils
from typer.core import TyperGroup
from typer.testing import CliRunner

from hecklr.cli import app, escape_option_help

runner = CliRunner()


@pytest.fixture(autouse=True)
def _wide_help(monkeypatch: pytest.MonkeyPatch) -> None:
    # Typer reads TERMINAL_WIDTH at import; a wide console keeps each help line on one row.
    monkeypatch.setattr(typer.rich_utils, "MAX_WIDTH", 400)


def _help(*command: str) -> str:
    result = runner.invoke(app, [*command, "--help"])
    assert result.exit_code == 0, result.output
    return click.unstyle(result.output)


def test_issue107_run_help_shows_its_defaults() -> None:
    assert "[default: " in _help("run")


@pytest.mark.parametrize(
    ("command", "note"),
    [
        ("report", "Formats to write (repeatable) [default: html]."),
        ("gate", "Exit code of an inconclusive verdict [default: gate.on_inconclusive]."),
        ("replay", "Extraction model [default: models.simulator]."),
        ("ui", "[default: your OS user name]."),
    ],
)
def test_issue107_hand_written_defaults_are_not_eaten_as_markup(command: str, note: str) -> None:
    assert note in _help(command)


def test_issue107_every_hand_written_default_reaches_the_help() -> None:
    group = typer.main.get_command(app)
    assert isinstance(group, TyperGroup)
    checked = 0
    for name, command in group.commands.items():
        if command.hidden or isinstance(command, TyperGroup):
            continue
        output = re.sub(r"[\s│]+", " ", _help(name))
        for param in command.params:
            raw = getattr(param, "help", None) or ""
            for note in re.findall(r"\[default: [^\]]*\]", raw.replace("\\[", "[")):
                assert re.sub(r"\s+", " ", note) in output, (name, note)
                checked += 1
    assert checked >= 15


def test_issue107_escaping_twice_changes_nothing() -> None:
    # `typer utils docs` (the CLI reference generator) runs hecklr.cli a second time.
    escape_option_help(app)

    assert "Formats to write (repeatable) [default: html]." in _help("report")


def test_issue107_bench_help_has_no_restructuredtext_markup() -> None:
    output = _help("bench", "run")

    assert "`results/<stack>.json`" in output
    assert "``" not in output
