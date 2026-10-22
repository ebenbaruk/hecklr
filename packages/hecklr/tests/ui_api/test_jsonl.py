import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hecklr.ui_api import jsonl
from hecklr.ui_api.jsonl import append_line


def test_lines_are_appended_whole(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    long = "x" * 100_000  # longer than any write buffer, still one write

    append_line(path, '{"a": 1}')
    append_line(path, long)

    assert path.read_text(encoding="utf-8").splitlines() == ['{"a": 1}', long]


def test_a_short_write_is_an_error_not_a_silent_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Only the module's view of `os` changes: patching os.write itself would break pytest.
    short = SimpleNamespace(
        open=os.open,
        close=os.close,
        write=lambda _fd, data: len(data) - 1,
        O_WRONLY=os.O_WRONLY,
        O_APPEND=os.O_APPEND,
        O_CREAT=os.O_CREAT,
    )
    monkeypatch.setattr(jsonl, "os", short)

    with pytest.raises(OSError, match="short write"):
        append_line(tmp_path / "queue.jsonl", "{}")
