"""Appending records to JSON Lines files that other processes append to as well."""

import os
from pathlib import Path


def append_line(path: Path, line: str) -> None:
    """Append ``line`` and a newline in one ``write`` on an ``O_APPEND`` descriptor.

    The miner (ADR 0013a) and the CLI append to the same files; one write of a whole line on an
    append-mode descriptor never interleaves with theirs on a local filesystem, whereas a buffered
    text file may split a long line into several writes.
    """
    data = (line + "\n").encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        written = os.write(descriptor, data)
    finally:
        os.close(descriptor)
    if written != len(data):
        raise OSError(f"short write to {path}: {written} of {len(data)} bytes")
