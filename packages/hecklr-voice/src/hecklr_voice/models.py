"""Download-on-demand for local model files (Kokoro, Silero VAD), kept in ``.hecklr/models``."""

import hashlib
import importlib
import importlib.util
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Final

import anyio.to_thread
import httpx2

from hecklr_voice.errors import VoiceConfigError, VoiceProviderError

DEFAULT_MODELS_DIR: Final = Path(".hecklr/models")
MODEL_THREADS: Final = 2
"""CPU threads per model inference. Each DSP worker runs one job at a time and half the cores
run DSP, so libraries that default to one thread per core (onnxruntime, CTranslate2) would
oversubscribe the machine many times over and starve the event loop of CPU."""
_CHUNK: Final = 1 << 20


async def ensure_model_file(
    url: str,
    path: Path,
    *,
    sha256: str | None = None,
    transport: httpx2.BaseTransport | None = None,
) -> Path:
    """Return ``path``, downloading ``url`` there first if it is missing.

    The download goes to a temporary file that is renamed only once complete (and, with
    ``sha256``, verified), so an interrupted download never leaves a corrupt model behind.
    Concurrent calls for one file are safe: within a process the first caller downloads and the
    others wait for it; separate processes each download to their own temporary file and the
    atomic rename publishes one complete copy.
    """
    await anyio.to_thread.run_sync(_download, url, path, sha256, transport)
    return path


_PATH_LOCKS: dict[Path, threading.Lock] = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path: Path) -> threading.Lock:
    with _PATH_LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(path.resolve(), threading.Lock())


def _download(
    url: str, path: Path, sha256: str | None, transport: httpx2.BaseTransport | None
) -> None:
    with _path_lock(path):
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique per call: another process may be downloading the same file right now.
        partial = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.part")
        try:
            _fetch(url, partial, sha256, transport)
            _publish(partial, path)
        finally:
            partial.unlink(missing_ok=True)


def _fetch(
    url: str, partial: Path, sha256: str | None, transport: httpx2.BaseTransport | None
) -> None:
    digest = hashlib.sha256()
    try:
        with (
            httpx2.Client(transport=transport, follow_redirects=True, timeout=60.0) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise VoiceProviderError(f"downloading {url}: HTTP {response.status_code}")
            with partial.open("wb") as out:
                for chunk in response.iter_bytes(_CHUNK):
                    digest.update(chunk)
                    out.write(chunk)
    except httpx2.HTTPError as exc:
        raise VoiceProviderError(f"downloading {url}: {exc}") from exc
    if sha256 is not None and digest.hexdigest() != sha256:
        raise VoiceProviderError(f"downloading {url}: checksum mismatch, refusing the file")


def _publish(partial: Path, path: Path) -> None:
    """Rename the complete download into place.

    Where replacing a file another process holds open is refused (Windows), the rename can fail
    because a competing download already published the same file: that file is complete (it was
    renamed into place whole), so it counts as success.
    """
    try:
        partial.replace(path)
    except OSError:
        if not path.exists():
            raise


def require_extra(module: str, what: str) -> None:
    """Refuse a local-model provider when its optional dependency is not installed."""
    if importlib.util.find_spec(module) is None:
        raise VoiceConfigError(f"{what} needs the extra: pip install 'hecklr-voice[local]'")


def onnx_session(path: Path, threads: int = MODEL_THREADS) -> Any:  # onnxruntime is untyped
    """An onnxruntime CPU session limited to ``threads`` threads, without busy-wait spinning.

    onnxruntime's default spins idle threads on every core, which steals CPU from the event
    loop and the other workers even when no inference is running.
    """
    onnxruntime = importlib.import_module("onnxruntime")
    options = onnxruntime.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.add_session_config_entry("session.intra_op.allow_spinning", "0")
    options.add_session_config_entry("session.inter_op.allow_spinning", "0")
    return onnxruntime.InferenceSession(
        str(path), sess_options=options, providers=["CPUExecutionProvider"]
    )
