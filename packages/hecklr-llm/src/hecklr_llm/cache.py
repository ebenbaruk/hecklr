"""On-disk response cache: re-running an unchanged call costs zero tokens."""

import sqlite3
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from types import TracebackType
from typing import Final, Self

import anyio
import anyio.to_thread

from hecklr_llm.keys import request_key
from hecklr_schemas import LLMProvider, LLMRequest, LLMResponse

DEFAULT_CACHE_PATH: Final = Path(".hecklr/cache/llm.sqlite3")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS responses (
    key TEXT PRIMARY KEY,
    model TEXT NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
)
"""


@dataclass
class _KeyLock:
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    users: int = 0


class CachedProvider:
    """Wraps any ``LLMProvider`` with a content-addressed SQLite cache.

    The key is the SHA-256 of the canonical request (model, system, messages, schema, temperature,
    max tokens, seed). A hit returns the stored reply with ``cached=True`` and ``cost_usd=0``.
    Concurrent identical requests wait for the first one instead of paying twice. Only successful
    responses are stored, and rows are never updated (append-only). SQLite runs in WAL mode so
    several processes can share one cache file.
    """

    def __init__(self, inner: LLMProvider, path: Path = DEFAULT_CACHE_PATH) -> None:
        self._inner = inner
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.execute(_SCHEMA)
        self._db_lock = threading.Lock()
        self._key_locks: dict[str, _KeyLock] = {}
        self.hits = 0
        self.misses = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Serve ``request`` from the cache, or call the wrapped provider and store the reply."""
        key = request_key(request)
        async with self._exclusive(key):
            stored = await anyio.to_thread.run_sync(self._get, key)
            if stored is not None:
                self.hits += 1
                return stored.model_copy(update={"cached": True, "cost_usd": Decimal(0)})
            self.misses += 1
            response = await self._inner.complete(request)
            await anyio.to_thread.run_sync(self._put, key, response)
            return response

    def close(self) -> None:
        """Close the database connection."""
        with self._db_lock:
            self._db.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @asynccontextmanager
    async def _exclusive(self, key: str) -> AsyncIterator[None]:
        # Per-key lock, dropped when its last user leaves so the dict cannot grow without bound.
        entry = self._key_locks.setdefault(key, _KeyLock())
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self._key_locks[key]

    def _get(self, key: str) -> LLMResponse | None:
        with self._db_lock:
            row = self._db.execute(
                "SELECT response FROM responses WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else LLMResponse.model_validate_json(row[0])

    def _put(self, key: str, response: LLMResponse) -> None:
        with self._db_lock:
            self._db.execute(
                "INSERT OR IGNORE INTO responses (key, model, response) VALUES (?, ?, ?)",
                (key, response.model, response.model_dump_json()),
            )
