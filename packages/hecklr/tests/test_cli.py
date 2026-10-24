import logging
import subprocess
import sys
from pathlib import Path

import click
import pytest
import structlog
import typer
from typer.testing import CliRunner

import hecklr
from hecklr import exit_codes
from hecklr.cli import app, configure_logging, main

runner = CliRunner()
IMPLEMENTED = {
    "init",
    "run",
    "replay",
    "doctor",
    "compare",
    "gate",
    "report",
    "calibrate",
    "mine",
    "ui",
    "fix",
    "bench",
}


def test_version_command_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == hecklr.__version__


def test_no_arguments_shows_help_listing_every_command() -> None:
    result = runner.invoke(app, [])

    for name in IMPLEMENTED:
        assert name in click.unstyle(result.output)


def test_spec_command_surface_is_complete() -> None:
    spec_commands = {
        "init", "run", "compare", "gate", "report", "replay",
        "mine", "fix", "calibrate", "bench", "ui", "doctor",
    }  # fmt: skip

    registered = set(typer.main.get_command(app).commands)  # type: ignore[attr-defined]  # a Typer group

    assert registered == spec_commands | {"version", "github"}  # github: the Action's, hidden
    assert spec_commands == IMPLEMENTED


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


CONSOLE_ENTRY = """
import os
import sys

import anyio

from hecklr.cli import main  # the console script's own import: hecklr.cli imports duckdb
from hecklr_voice.pool import run_dsp

print("ran", file=open(sys.argv[1], "a"))
print(anyio.run(run_dsp, os.getpid) != os.getpid())
"""


def test_issue31_dsp_workers_start_under_an_entry_script_that_imports_the_cli(
    tmp_path: Path,
) -> None:
    # DSP workers never run the parent's main file (ADR 00hva), so the console script needs no
    # workaround to keep duckdb's import out of them (#82).
    pytest.importorskip("hecklr_voice")
    script = tmp_path / "hecklr_entry.py"
    script.write_text(CONSOLE_ENTRY, encoding="utf-8")
    runs = tmp_path / "runs.txt"

    done = subprocess.run(  # noqa: S603 -- this interpreter running the test's own script
        [sys.executable, str(script), str(runs)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout == "True\n"
    assert runs.read_text() == "ran\n", "the entry script ran once, in the parent only"
