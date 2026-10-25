"""Build the UI app from a Hecklr checkout on the first ``hecklr ui`` (issue #106).

A wheel ships the built app (ADR 0016a). A checkout has only the sources in ``ui/``: the build
output is git-ignored, so without this the documented checkout install failed on ``hecklr ui``.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Final

BUILD_COMMAND: Final = "npm --prefix ui ci && npm --prefix ui run build"
"""The manual build, run from the checkout's root."""
NODE_REQUIREMENT: Final = "Node.js 20.19 or newer"
"""What Vite, the UI's build tool, needs."""
UI_SOURCE: Final = Path(__file__).resolve().parents[5] / "ui"
"""Where a checkout keeps the UI's sources (``packages/hecklr/src/hecklr/ui_api`` → ``ui/``)."""


class UIBuildError(RuntimeError):
    """The UI app is not built and could not be built here."""


def is_built(static_dir: Path) -> bool:
    """Whether ``static_dir`` holds a built app."""
    return (static_dir / "index.html").is_file()


def checkout_source() -> Path | None:
    """The checkout's ``ui/`` sources, or ``None`` in an install that has none."""
    return UI_SOURCE if (UI_SOURCE / "package.json").is_file() else None


def build_app(source: Path, static_dir: Path) -> None:
    """Install the UI's dependencies (unless present) and build the app into ``static_dir``.

    npm writes to the terminal, so the reader sees progress and any error as npm prints it.

    Raises:
        UIBuildError: npm is not on PATH, a step failed, or the build wrote no app.
    """
    npm = shutil.which("npm")
    if npm is None:
        raise UIBuildError(
            f"the UI app is not built yet, and building it needs {NODE_REQUIREMENT} (npm on "
            f"PATH): install Node.js and run `hecklr ui` again, or run `{BUILD_COMMAND}` in "
            f"{source.parent}"
        )
    steps = [] if (source / "node_modules").is_dir() else [[npm, "ci"]]
    for step in [*steps, [npm, "run", "build"]]:
        # The arguments are fixed and npm's path comes from PATH, as when typed by hand.
        completed = subprocess.run(step, cwd=source, check=False)  # noqa: S603
        if completed.returncode != 0:
            raise UIBuildError(
                f"`npm {' '.join(step[1:])}` in {source} failed with exit code "
                f"{completed.returncode}; fix the error above, then run `hecklr ui` again"
            )
    if not is_built(static_dir):
        raise UIBuildError(f"`npm run build` in {source} did not write {static_dir / 'index.html'}")
