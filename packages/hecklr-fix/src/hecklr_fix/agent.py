"""Start and stop the agent under test for one pass: the agent-reload contract (ADR 0015a).

The user gives a command template, e.g. ``{python} agent/serve.py --port {port}``. For each pass
the fixer runs it in the workspace copy (so the agent reads the candidate's files), with
``{port}`` a free port the copy's adapter URLs point at, ``{python}`` this interpreter and
``{project}`` the copy's directory. The agent is ready when its port accepts a TCP connection;
it is stopped with SIGTERM to its own process group (then SIGKILL), so a wrapper such as
``uv run`` and the server it starts both go.
"""

import os
import shlex
import signal
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio
from anyio.abc import Process

from hecklr_fix.errors import FixError

PLACEHOLDERS: Final = ("port", "python", "project")
STOP_GRACE_S: Final = 10.0
POLL_S: Final = 0.2


def free_port() -> int:
    """An ephemeral localhost port that was free a moment ago.

    Another process can take it before the agent binds it; the agent then fails to start and the
    readiness check reports its exit.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@dataclass(frozen=True, slots=True)
class AgentCommand:
    """A command template that starts the agent on a given port."""

    template: str
    ready_timeout_s: float = 60.0

    def __post_init__(self) -> None:
        if "{port}" not in self.template:
            raise FixError("--agent-cmd must contain {port}: the agent has to listen where told")
        self.argv(port=1, project=Path())  # fail on unknown placeholders before any run

    def argv(self, *, port: int, project: Path) -> list[str]:
        """The command for one launch, split like a POSIX shell would (no shell runs it).

        The template is split before the placeholders are filled in, so a value containing a
        space (a project under ``My Drive``) stays one argument.
        """
        try:
            tokens = shlex.split(self.template)
        except ValueError as exc:
            raise FixError(f"--agent-cmd: cannot split {self.template!r}: {exc}") from exc
        try:
            return [
                token.format(port=port, python=sys.executable, project=project) for token in tokens
            ]
        except (KeyError, IndexError, ValueError) as exc:
            raise FixError(
                f"--agent-cmd: bad placeholder {exc}; known: "
                + ", ".join(f"{{{name}}}" for name in PLACEHOLDERS)
                + " (write {{ and }} for literal braces)"
            ) from exc

    @asynccontextmanager
    async def launch(self, workspace: Path, host: str, port: int, log: Path) -> AsyncIterator[None]:
        """Run the agent from ``workspace`` until the block ends, once it accepts connections.

        Raises:
            FixError: the agent exits or does not listen within ``ready_timeout_s``.
        """
        argv = self.argv(port=port, project=workspace)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("ab") as output:
            try:
                process = await anyio.open_process(
                    argv, cwd=workspace, stdout=output, stderr=output, start_new_session=True
                )
            except OSError as exc:
                raise FixError(f"cannot start the agent ({argv[0]}): {exc}") from exc
            try:
                await _wait_until_listening(process, host, port, self.ready_timeout_s, log)
                yield
            finally:
                await _stop(process)


async def _wait_until_listening(
    process: Process, host: str, port: int, timeout_s: float, log: Path
) -> None:
    with anyio.move_on_after(timeout_s):
        while True:
            if process.returncode is not None:
                raise FixError(
                    f"the agent exited with code {process.returncode} before listening on "
                    f"port {port}; see {log}"
                )
            try:
                stream = await anyio.connect_tcp(host, port)
            except OSError:
                await anyio.sleep(POLL_S)
                continue
            await stream.aclose()
            return
    raise FixError(f"the agent did not listen on {host}:{port} within {timeout_s:.0f} s; see {log}")


async def _stop(process: Process) -> None:
    with anyio.CancelScope(shield=True):
        _signal_group(process, signal.SIGTERM)
        with anyio.move_on_after(STOP_GRACE_S):
            await process.wait()
            return
        _signal_group(process, signal.SIGKILL)
        await process.wait()


def _signal_group(process: Process, sig: signal.Signals) -> None:
    # The agent was started in its own session, so its process group id is its pid: this reaches
    # the agent and its children and nothing else.
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return
