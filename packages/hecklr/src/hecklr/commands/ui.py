"""``hecklr ui``: start the local web UI over the project's stored runs (spec, "Local UI")."""

import getpass
import threading
import webbrowser
from typing import Annotated, Final

import typer
import uvicorn
from rich.markup import escape

from hecklr import exit_codes
from hecklr.commands._common import DEFAULT_CONFIG, ConfigPath, fail, load_project_or_exit, stdout
from hecklr.ui_api import STATIC_DIR, create_app

DEFAULT_PORT: Final = 8484
BUILD_HINT: Final = "npm --prefix ui ci && npm --prefix ui run build"


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
    if static_dir is not None and not (static_dir / "index.html").is_file():
        raise fail(
            f"the UI app is not built into {static_dir}; build it with `{BUILD_HINT}` "
            "from a Hecklr checkout, or run `hecklr ui --dev` with `npm run dev` in ui/",
            exit_codes.NOT_IMPLEMENTED,
        )
    app = create_app(project, user=user or getpass.getuser(), static_dir=static_dir)
    url = f"http://{host}:{port}/"
    stdout.print(f"Hecklr UI for {escape(str(project.config_path))} at [bold]{url}[/]")
    if dev:
        stdout.print("API only: run `npm run dev` in ui/ and open the URL Vite prints.")
    elif open_browser:
        # The browser takes longer to start than uvicorn takes to bind, so the page loads.
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host=host, port=port, log_level="warning")
