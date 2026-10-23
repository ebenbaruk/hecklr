"""The stack's agent as a child process: started, waited for, asked for its spend, stopped.

Only the process this module started is ever signalled (several benchmark or test runs may share
a machine). Its combined output goes to a log file, which is also where a ``ready.line`` is
looked for.
"""

import os
import re
import socket
import subprocess
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

import httpx2

from hecklr.commands.bench.spec import Ready

POLL_S: Final = 0.2
STOP_TIMEOUT_S: Final = 30.0
"""Long enough for an agent to leave its rooms and stop its containers on SIGTERM."""


class StackError(Exception):
    """The stack could not be measured: it did not start, died, or misreported its spend."""


def free_port() -> int:
    """A 127.0.0.1 TCP port that was free a moment ago.

    Another process could take it before the agent binds it; the agent then fails to start and
    the run stops with a ``StackError`` instead of measuring something else.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@contextmanager
def agent_process(
    command: Sequence[str],
    *,
    env: Mapping[str, str],
    ready: Ready,
    cwd: Path,
    log_path: Path,
) -> Iterator[subprocess.Popen[bytes]]:
    """Run ``command`` until the block ends, yielding once it is ready.

    Raises:
        StackError: the process exited or stayed unready for ``ready.timeout_s``.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("wb") as log:
        process = subprocess.Popen(  # noqa: S603 -- a stack file's command, run without a shell
            list(command),
            cwd=cwd,
            env={**os.environ, **env},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_ready(process, ready, log_path)
            yield process
        finally:
            stop(process)


def stop(process: subprocess.Popen[bytes]) -> None:
    """SIGTERM, then SIGKILL if the process has not exited within ``STOP_TIMEOUT_S``."""
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=STOP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _wait_ready(process: subprocess.Popen[bytes], ready: Ready, log_path: Path) -> None:
    pattern = re.compile(ready.line) if ready.line is not None else None
    deadline = time.monotonic() + ready.timeout_s
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise StackError(
                f"the agent exited with code {process.returncode} before it was ready; "
                f"see {log_path}"
            )
        if pattern is not None and pattern.search(log_path.read_text("utf-8", errors="replace")):
            return
        if ready.http is not None and _answers(ready.http):
            return
        time.sleep(POLL_S)
    raise StackError(f"the agent was not ready within {ready.timeout_s:.0f} s; see {log_path}")


def _answers(url: str) -> bool:
    try:
        return httpx2.get(url, timeout=2).status_code == 200
    except httpx2.TransportError:
        return False  # not listening yet


def read_spend(url: str) -> Decimal:
    """The agent's own spend so far, from the ``spent_usd`` field of ``GET url``.

    Raises:
        StackError: the agent did not answer, or answered without a number.
    """
    try:
        body = httpx2.get(url, timeout=10).raise_for_status().json()
        return Decimal(str(body["spent_usd"]))
    except (httpx2.HTTPError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
        raise StackError(f"cannot read the agent's spend from {url}: {exc!r}") from exc
