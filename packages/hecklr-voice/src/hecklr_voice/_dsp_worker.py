"""A DSP worker process: ``python -m hecklr_voice._dsp_worker``, driven by :mod:`hecklr_voice.pool`.

Protocol over stdin/stdout. Every request is a frame: its length and a newline, then a pickle.
The first is the parent's ``sys.path``, then one ``(func, args)`` per job. Every reply is a line
``RETURN <length>`` or ``EXCEPTION <length>``, then a pickle of the result or the exception. The
worker exits at end of input.

Unlike ``anyio.to_process`` workers it never runs the parent's main file, and its own module
stays ``__main__``: libraries such as duckdb refuse to import in a process without one (#31).
"""

import os
import pickle
import sys
from typing import BinaryIO


def serve(requests: BinaryIO, replies: BinaryIO) -> None:
    """Adopt the parent's import path, then run jobs until ``requests`` ends."""
    path = _receive(requests)
    if path is None:
        return
    sys.path[:] = pickle.loads(path)  # noqa: S301 -- the parent process, over a private pipe
    _reply(replies, ok=True, value=None)
    while (request := _receive(requests)) is not None:
        _reply(replies, *_run(request))


def _receive(requests: BinaryIO) -> bytes | None:
    header = requests.readline()
    return requests.read(int(header)) if header else None


def _run(request: bytes) -> tuple[bool, object]:
    try:
        func, args = pickle.loads(request)  # noqa: S301 -- the parent process, over a private pipe
        return True, func(*args)
    except BaseException as exc:  # noqa: BLE001 -- every failure is the caller's, raised there
        return False, exc


def _reply(replies: BinaryIO, ok: bool, value: object) -> None:
    try:
        payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:  # noqa: BLE001 -- reported to the caller instead of the value
        ok = False
        payload = pickle.dumps(RuntimeError(f"DSP job result could not be pickled: {exc!r}"))
    status = b"RETURN" if ok else b"EXCEPTION"
    replies.write(b"%s %d\n" % (status, len(payload)) + payload)
    replies.flush()


def main() -> None:
    """Serve the parent over the original stdin/stdout, with fds 1 and 2 sent to the null device.

    Anything a job or a native library prints would otherwise corrupt the reply stream (fd 1) or
    fill a pipe nobody reads (fd 2) and hang the worker.
    """
    replies = os.fdopen(os.dup(1), "wb")
    null = os.open(os.devnull, os.O_WRONLY)
    os.dup2(null, 1)
    os.dup2(null, 2)
    serve(sys.stdin.buffer, replies)


if __name__ == "__main__":
    main()
