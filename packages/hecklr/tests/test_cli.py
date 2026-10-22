import logging
import sys
import types

import click
import pytest
import structlog
import typer
from typer.testing import CliRunner

import hecklr
from hecklr import exit_codes
from hecklr.cli import app, configure_logging, main
from hecklr.commands.stubs import PENDING_COMMANDS

runner = CliRunner()
IMPLEMENTED = {"init", "run", "replay", "doctor", "compare", "gate", "report", "calibrate", "ui"}


def test_version_command_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == hecklr.__version__


def test_no_arguments_shows_help_listing_every_command() -> None:
    result = runner.invoke(app, [])

    for name in [*IMPLEMENTED, *PENDING_COMMANDS]:
        assert name in click.unstyle(result.output)


@pytest.mark.parametrize("command", sorted(PENDING_COMMANDS))
def test_pending_command_fails_loudly_until_implemented(command: str) -> None:
    result = runner.invoke(app, [command, "--some-flag", "value"])

    assert result.exit_code == exit_codes.NOT_IMPLEMENTED
    assert "not implemented yet" in click.unstyle(result.output)


def test_spec_command_surface_is_complete() -> None:
    spec_commands = {
        "init", "run", "compare", "gate", "report", "replay",
        "mine", "fix", "calibrate", "bench", "ui", "doctor",
    }  # fmt: skip

    registered = set(typer.main.get_command(app).commands)  # type: ignore[attr-defined]  # a Typer group

    assert registered == spec_commands | {"version", "github"}  # github: the Action's, hidden
    assert set(PENDING_COMMANDS) == spec_commands - IMPLEMENTED


def test_exit_codes_never_collide_with_the_gate_codes() -> None:
    gate = {0, 1, 2, 3}

    assert {exit_codes.BUDGET, exit_codes.CONFIG_ERROR, exit_codes.NOT_IMPLEMENTED}.isdisjoint(gate)


@pytest.mark.parametrize(
    ("verbosity", "shown", "hidden"),
    [(0, logging.WARNING, logging.INFO), (1, logging.INFO, logging.DEBUG), (5, logging.DEBUG, 0)],
)
def test_verbosity_sets_the_log_level(
    verbosity: int, shown: int, hidden: int, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(verbosity)
    log = structlog.get_logger("test")

    log.log(shown, "visible-line", run_id="r1")
    if hidden:
        log.log(hidden, "hidden-line")

    err = capsys.readouterr().err
    assert "visible-line" in err
    assert "run_id=r1" in err
    assert "hidden-line" not in err


def test_console_script_entry_point_runs_the_app(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["hecklr", "version"])

    with pytest.raises(SystemExit) as exit_info:
        main()

    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == hecklr.__version__


def test_console_entry_gives_dsp_workers_no_script_to_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # DSP workers re-run the parent's main file without a __main__ module, where importing
    # hecklr.cli (duckdb) fails; the console script must not be offered to them.
    entry = types.ModuleType("__main__")
    entry.__file__ = "/venv/bin/hecklr"
    monkeypatch.setitem(sys.modules, "__main__", entry)
    monkeypatch.setattr(sys, "argv", ["hecklr", "version"])

    with pytest.raises(SystemExit) as exited:
        main()

    assert exited.value.code == 0
    assert getattr(entry, "__file__", None) is None
