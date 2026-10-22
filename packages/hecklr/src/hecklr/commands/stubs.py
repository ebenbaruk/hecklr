"""Commands from the spec that other workstreams implement; each fails loudly until then."""

from collections.abc import Callable
from typing import Final

import typer

from hecklr import __version__, exit_codes
from hecklr.commands._common import stderr

PENDING_COMMANDS: Final[dict[str, str]] = {
    "fix": "Generate and validate prompt patches for a failure cluster.",
    "bench": "Run or reproduce the public voice benchmark.",
    "ui": "Start the local web UI.",
}
"""Command name -> one-line help, in the order the spec lists them."""


def register(app: typer.Typer) -> None:
    """Add every pending command to ``app``."""
    for name, help_text in PENDING_COMMANDS.items():
        # Accept any arguments so `hecklr gate --suite ci` reports "not implemented" instead of a
        # usage error that would suggest the flag is wrong.
        app.command(
            name=name,
            help=help_text,
            context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
        )(_make_stub(name))


def _make_stub(name: str) -> Callable[[typer.Context], None]:
    def stub(ctx: typer.Context) -> None:
        stderr.print(f"[bold red]hecklr {name}[/] is not implemented yet (Hecklr {__version__}).")
        raise typer.Exit(code=exit_codes.NOT_IMPLEMENTED)

    return stub
