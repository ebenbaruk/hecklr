"""Concurrent first downloads of one model file (issues #57, #81) against a local HTTP server.

Threads of one process (the agent's start-up synthesizing in parallel) and separate processes
(several agents or workers on a fresh machine) all ask for the same missing file at once. Every
caller must get the complete file and no partial download may be left behind.
"""

import hashlib
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import ClassVar

import anyio
import pytest

from hecklr_voice import models
from hecklr_voice.models import ensure_model_file

pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

BODY = bytes(range(256)) * 4096
"""1 MiB: several chunks, so concurrent downloads overlap."""
DIGEST = hashlib.sha256(BODY).hexdigest()


class SlowFile(BaseHTTPRequestHandler):
    """Serves ``BODY`` in 64 KiB pieces with pauses, so every download is in flight at once."""

    requests: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        SlowFile.requests.append(self.path)
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        for start in range(0, len(BODY), 1 << 16):
            self.wfile.write(BODY[start : start + (1 << 16)])
            time.sleep(0.005)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the test output clean."""


class Server(ThreadingHTTPServer):
    request_queue_size = 64
    """Every concurrent download connects at once; the default backlog of 5 resets some."""
    daemon_threads = True


@pytest.fixture
def model_url() -> Iterator[str]:
    SlowFile.requests.clear()
    server = Server(("127.0.0.1", 0), SlowFile)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/kokoro-v1.0.onnx"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _leftovers(directory: Path) -> list[str]:
    return sorted(path.name for path in directory.iterdir() if path.name.endswith(".part"))


@pytest.mark.anyio
async def test_issue57_concurrent_first_downloads_in_one_process_all_succeed(
    model_url: str, tmp_path: Path
) -> None:
    target = tmp_path / "kokoro" / "kokoro-v1.0.onnx"
    results: list[Path] = []

    async def fetch() -> None:
        results.append(await ensure_model_file(model_url, target, sha256=DIGEST))

    async with anyio.create_task_group() as group:
        for _ in range(16):
            group.start_soon(fetch)

    assert results == [target] * 16
    assert target.read_bytes() == BODY
    assert _leftovers(target.parent) == []
    assert len(SlowFile.requests) == 1, "threads of one process download the file once"


WORKER = """
import sys, threading
from functools import partial
from pathlib import Path
import anyio
from hecklr_voice.models import ensure_model_file

url, target, digest = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
errors = []

def fetch():
    try:
        anyio.run(partial(ensure_model_file, url, target, sha256=digest))
    except BaseException as exc:
        errors.append(repr(exc))

threads = [threading.Thread(target=fetch) for _ in range(4)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
print(errors)
sys.exit(1 if errors else 0)
"""


def test_issue81_concurrent_first_downloads_across_processes_all_succeed(
    model_url: str, tmp_path: Path
) -> None:
    target = tmp_path / "kokoro" / "kokoro-v1.0.onnx"
    script = tmp_path / "fetch.py"
    script.write_text(WORKER)
    workers = [
        subprocess.Popen(  # noqa: S603 -- this interpreter running the test's own script
            [sys.executable, str(script), model_url, str(target), DIGEST],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        for _ in range(4)
    ]
    outputs = [worker.communicate(timeout=60)[0] for worker in workers]

    assert [worker.returncode for worker in workers] == [0] * 4, outputs
    assert target.read_bytes() == BODY
    assert _leftovers(target.parent) == []


def test_issue81_a_lost_rename_race_counts_as_success_when_the_target_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Where replacing a file another process holds open is refused (Windows), the rename can
    # fail after a competitor already published the same complete file.
    target = tmp_path / "model.bin"

    def competitor_won(self: Path, destination: Path) -> Path:
        destination.write_bytes(BODY)
        raise PermissionError(13, "in use", str(destination))

    monkeypatch.setattr(Path, "replace", competitor_won)
    models._publish(tmp_path / "model.bin.1.part", target)
    assert target.read_bytes() == BODY


def test_issue81_a_failed_rename_without_a_target_is_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(self: Path, destination: Path) -> Path:
        raise PermissionError(13, "denied", str(destination))

    monkeypatch.setattr(Path, "replace", refused)
    with pytest.raises(PermissionError):
        models._publish(tmp_path / "model.bin.1.part", tmp_path / "model.bin")
