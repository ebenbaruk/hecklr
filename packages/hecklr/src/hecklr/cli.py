"""The ``hecklr`` command-line entry point.

Each command lives in its own module under ``hecklr.commands`` and is registered here, so
workstreams adding commands touch one new file and one line. Commands not built yet are stubs
(``hecklr.commands.stubs``) that exit with ``EX_UNAVAILABLE``.
"""

import logging
import sys
from typing import Annotated, Any

import structlog
import typer

from hecklr import __version__
from hecklr.commands import stubs
from hecklr.commands.calibrate import calibrate
from hecklr.commands.compare import compare
from hecklr.commands.doctor import doctor
from hecklr.commands.gate import gate
from hecklr.commands.init import init
from hecklr.commands.replay import replay
from hecklr.commands.report import report
from hecklr.commands.run import run

app = typer.Typer(
    name="hecklr",
    help="The crash-test lab for conversational AI agents.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def configure(
    verbose: Annotated[
        int, typer.Option("--verbose", "-v", count=True, help="Show harness logs (-vv: debug).")
    ] = 0,
) -> None:
    """The crash-test lab for conversational AI agents."""
    configure_logging(verbose)


def configure_logging(verbosity: int) -> None:
    """Harness logs go to stderr: warnings by default, ``-v`` info, ``-vv`` debug."""
    level = (logging.WARNING, logging.INFO, logging.DEBUG)[min(verbosity, 2)]
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # Looked up on every call, so lines written during the live progress display go through
        # Rich's redirected stderr instead of tearing the progress bar.
        logger_factory=_stderr_logger,
    )


def _stderr_logger(*args: Any) -> structlog.PrintLogger:
    return structlog.PrintLogger(sys.stderr)


@app.command()
def version() -> None:
    """Print the installed Hecklr version."""
    typer.echo(__version__)


app.command(help="Interactive setup: write hecklr.yaml, a starter world, scenarios and personas.")(
    init
)
app.command(help="Run a suite against the agent and write results.")(run)
app.command(help="Statistical comparison of two runs.")(compare)
app.command(help="Compare against the stored baseline and set the CI exit code.")(gate)
app.command(help="Build HTML, Markdown, JSON or JUnit output for a run.")(report)
app.command(help="Self-tests: A/A false-positive rate, judge agreement, voice loopback.")(calibrate)
app.command(help="Convert real transcripts into replay scenarios.")(replay)
app.command(help="Check keys, audio dependencies, ports and agent reachability.")(doctor)
stubs.register(app)


def main() -> None:
    """Console-script entry point."""
    app()
