"""Download-on-demand for local model files (Kokoro, Silero VAD), kept in ``.hecklr/models``."""

import hashlib
import importlib.util
import os
from pathlib import Path
from typing import Final

import anyio.to_thread
import httpx2

from hecklr_voice.errors import VoiceConfigError, VoiceProviderError

DEFAULT_MODELS_DIR: Final = Path(".hecklr/models")
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
    """
    await anyio.to_thread.run_sync(_download, url, path, sha256, transport)
    return path


def _download(
    url: str, path: Path, sha256: str | None, transport: httpx2.BaseTransport | None
) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(f"{path.name}.{os.getpid()}.part")
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
        if sha256 is not None and digest.hexdigest() != sha256:
            raise VoiceProviderError(f"downloading {url}: checksum mismatch, refusing the file")
        partial.replace(path)
    except httpx2.HTTPError as exc:
        raise VoiceProviderError(f"downloading {url}: {exc}") from exc
    finally:
        partial.unlink(missing_ok=True)


def require_extra(module: str, what: str) -> None:
    """Refuse a local-model provider when its optional dependency is not installed."""
    if importlib.util.find_spec(module) is None:
        raise VoiceConfigError(f"{what} needs the extra: pip install 'hecklr-voice[local]'")
