"""The ``hecklr`` command-line entry point.

Wave 0 ships the command surface from the spec so scripts and docs can target stable names. Each
command except ``version`` is a stub that fails loudly until its workstream (WS7 and others)
implements it.
"""

from collections.abc import Callable
from typing import Final

import typer
from rich.console import Console

from hecklr import __version__

# EX_UNAVAILABLE from sysexits.h: distinct from the gate's 0-3 codes, so a stub can never be
# mistaken for a pass, a regression or an inconclusive verdict.
NOT_IMPLEMENTED_EXIT_CODE: Final = 69

# Command name -> one-line help, in the order the spec lists them.
PLANNED_COMMANDS: Final[dict[str, str]] = {
    "init": "Interactive setup: write hecklr.yaml, a starter world, scenarios and personas.",
    "run": "Run a suite against the agent and write results.",
    "compare": "Statistical comparison of two runs.",
    "gate": "Compare against the stored baseline and set the CI exit code.",
    "report": "Build HTML, Markdown, JSON or JUnit output for a run.",
    "replay": "Convert real transcripts into replay scenarios.",
    "mine": "Mine production logs into failure clusters and proposed scenarios.",
    "fix": "Generate and validate prompt patches for a failure cluster.",
    "calibrate": "Self-tests: A/A false-positive rate, judge agreement, voice loopback.",
    "bench": "Run or reproduce the public voice benchmark.",
    "ui": "Start the local web UI.",
    "doctor": "Check keys, audio dependencies, ports and agent reachability.",
}

app = typer.Typer(
    name="hecklr",
    help="The crash-test lab for conversational AI agents.",
    no_args_is_help=True,
    add_completion=False,
)
_stderr = Console(stderr=True)


@app.command()
def version() -> None:
    """Print the installed Hecklr version."""
    typer.echo(__version__)


def _make_stub(name: str) -> Callable[[typer.Context], None]:
    def stub(ctx: typer.Context) -> None:
        _stderr.print(f"[bold red]hecklr {name}[/] is not implemented yet (Hecklr {__version__}).")
        raise typer.Exit(code=NOT_IMPLEMENTED_EXIT_CODE)

    return stub


for _name, _help in PLANNED_COMMANDS.items():
    # Accept any arguments so `hecklr run --suite ci` reports "not implemented" instead of a
    # usage error that would suggest the flag is wrong.
    app.command(
        name=_name,
        help=_help,
        context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    )(_make_stub(_name))


def main() -> None:
    """Console-script entry point."""
    app()
