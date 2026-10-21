import pytest
from typer.testing import CliRunner

import hecklr
from hecklr.cli import NOT_IMPLEMENTED_EXIT_CODE, PLANNED_COMMANDS, app

runner = CliRunner()


def test_version_command_prints_package_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == hecklr.__version__


def test_no_arguments_shows_help_listing_every_command() -> None:
    result = runner.invoke(app, [])

    for name in PLANNED_COMMANDS:
        assert name in result.output


@pytest.mark.parametrize("command", sorted(PLANNED_COMMANDS))
def test_planned_command_fails_loudly_until_implemented(command: str) -> None:
    result = runner.invoke(app, [command, "--some-flag", "value"])

    assert result.exit_code == NOT_IMPLEMENTED_EXIT_CODE
    assert "not implemented yet" in result.output


def test_spec_command_surface_is_complete() -> None:
    spec_commands = {
        "init", "run", "compare", "gate", "report", "replay",
        "mine", "fix", "calibrate", "bench", "ui", "doctor",
    }  # fmt: skip

    assert set(PLANNED_COMMANDS) == spec_commands
