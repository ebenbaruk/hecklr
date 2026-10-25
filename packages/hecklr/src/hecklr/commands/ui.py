"""``hecklr ui``: start the local web UI over the project's stored runs (spec, "Local UI")."""

import getpass
import ipaddress
import threading
import webbrowser
from pathlib import Path
from typing import Annotated, Final

import typer
import uvicorn
from rich.markup import escape

from hecklr import exit_codes
from hecklr.commands._common import DEFAULT_CONFIG, ConfigPath, fail, load_project_or_exit, stdout
from hecklr.ui_api import LOCAL_HOSTS, STATIC_DIR, create_app
from hecklr.ui_api.build import (
    BUILD_COMMAND,
    UIBuildError,
    build_app,
    checkout_source,
    is_built,
)

DEFAULT_PORT: Final = 8484


def ui(
    config: ConfigPath = DEFAULT_CONFIG,
    host: Annotated[
        str, typer.Option(help="Interface to bind; the UI has no login, keep it local.")
    ] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to serve on.")] = DEFAULT_PORT,
    user: Annotated[
        str | None,
        typer.Option(help="Default labeler and reviewer name [default: your OS user name]."),
    ] = None,
    open_browser: Annotated[
        bool, typer.Option("--open/--no-open", help="Open the UI in a browser.")
    ] = True,
    dev: Annotated[
        bool,
        typer.Option(help="Serve the API only, for `npm run dev` in ui/ (it proxies /api)."),
    ] = False,
) -> None:
    """Start the local web UI."""
    project = load_project_or_exit(config)
    static_dir = None if dev else STATIC_DIR
    if static_dir is not None and not is_built(static_dir):
        _build(static_dir)
    app = create_app(
        project,
        user=user or getpass.getuser(),
        static_dir=static_dir,
        allowed_hosts=allowed_hosts(host),
    )
    url = f"http://{host}:{port}/"
    stdout.print(f"Hecklr UI for {escape(str(project.config_path))} at [bold]{url}[/]")
    if dev:
        stdout.print("API only: run `npm run dev` in ui/ and open the URL Vite prints.")
    elif open_browser:
        # The browser takes longer to start than uvicorn takes to bind, so the page loads.
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")


def _build(static_dir: Path) -> None:
    """Build the app on the first start from a checkout; an installed wheel ships it built."""
    source = checkout_source()
    if source is None:
        raise fail(
            f"the UI app is missing from this install ({static_dir}): reinstall `hecklr[ui]`, "
            f"or build it with `{BUILD_COMMAND}` from a Hecklr checkout",
            exit_codes.NOT_IMPLEMENTED,
        )
    stdout.print(f"Building the UI app from {escape(str(source))} (first start only)...")
    try:
        build_app(source, static_dir)
    except UIBuildError as exc:
        raise fail(str(exc), exit_codes.NOT_IMPLEMENTED) from exc


def allowed_hosts(bind: str) -> list[str]:
    """The ``Host`` names the server answers when bound to ``bind``.

    A local bind answers only local names (DNS-rebinding guard); a named interface adds its own
    name; a wildcard bind is a deliberate choice to serve the network, under any name it has.
    """
    try:
        address = ipaddress.ip_address(bind)
    except ValueError:
        return list(dict.fromkeys([*LOCAL_HOSTS, bind]))  # a host name, e.g. `myhost.lan`
    if address.is_unspecified:
        return ["*"]
    named = f"[{address}]" if address.version == 6 else str(address)
    return list(dict.fromkeys([*LOCAL_HOSTS, named]))
