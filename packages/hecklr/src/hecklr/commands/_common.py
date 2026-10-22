"""What the command modules share: consoles, test injection and configuration error handling."""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Final

import typer
from rich.console import Console
from rich.markup import escape

from hecklr import exit_codes
from hecklr.channels import ChannelUnavailableError
from hecklr.compose import UnknownBaselineError, load_run_project
from hecklr.results import RunLookupError
from hecklr_adapters import AdapterConfigError
from hecklr_core import ConfigError, Project, RunNotFoundError
from hecklr_grade import CustomGraderError, InvalidCriterionError, UnknownRubricError
from hecklr_llm import ProviderConfigError, UnknownPriceError, UnknownProviderError
from hecklr_schemas import LLMProvider
from hecklr_world import WorldConfigError

CONFIG_ERRORS: Final = (
    ConfigError,
    WorldConfigError,
    ChannelUnavailableError,
    UnknownBaselineError,
    AdapterConfigError,
    InvalidCriterionError,
    UnknownRubricError,
    CustomGraderError,
    UnknownPriceError,
    UnknownProviderError,
    ProviderConfigError,
)
"""Problems the user fixes in ``hecklr.yaml``, an authored file, a flag or the environment."""

stdout = Console()
stderr = Console(stderr=True)


@dataclass(frozen=True, slots=True)
class CliOverrides:
    """Dependencies a caller can inject through Click's context object (``obj``).

    The console script passes nothing; tests pass ``CliRunner().invoke(app, args,
    obj=CliOverrides(llm_providers={"fake": FakeLLM(...)}))`` to script the models.
    """

    llm_providers: Mapping[str, LLMProvider] = field(default_factory=dict)


def overrides(ctx: typer.Context) -> CliOverrides:
    """The injected overrides, or the defaults."""
    return ctx.obj if isinstance(ctx.obj, CliOverrides) else CliOverrides()


def fail(message: str, code: int) -> typer.Exit:
    """Print ``message`` to stderr and return the exit to raise."""
    stderr.print(f"[bold red]error:[/] {escape(message)}", soft_wrap=True)
    return typer.Exit(code=code)


@contextmanager
def config_errors(code: int = exit_codes.CONFIG_ERROR) -> Iterator[None]:
    """Turn configuration problems and unknown runs into a readable message and ``code``."""
    try:
        yield
    except (*CONFIG_ERRORS, RunLookupError, RunNotFoundError) as exc:
        raise fail(str(exc), code) from exc


ConfigPath = Annotated[Path, typer.Option("--config", "-c", help="Path to hecklr.yaml.")]
"""The ``--config`` option every project command takes."""
DEFAULT_CONFIG: Final = Path("hecklr.yaml")


def load_project_or_exit(config: Path, code: int = exit_codes.CONFIG_ERROR) -> Project:
    """The project at ``config``, or a readable error exiting with ``code``."""
    with config_errors(code):
        return load_run_project(config)
