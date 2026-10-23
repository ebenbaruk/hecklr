"""A cap on how much text one agent turn may carry, around any ``ChatTarget`` (ADR 00hra).

An agent that floods (a streaming loop, a model that never stops, a debug dump in the reply)
would otherwise fill the result store and the HTML report: 20 MB replies made a 33-trial run
write 2.9 GB (issue #34). Past the cap the head of the reply is kept, so the transcript shows
what the agent sent, and the turn fails as an ``AgentError``.
"""

from collections.abc import AsyncGenerator, AsyncIterator
from typing import Final

from hecklr_adapters.errors import AgentError
from hecklr_schemas import AgentChunk, ChatSession, ChatTarget, SessionContext, SessionSummary

DEFAULT_MAX_REPLY_BYTES: Final = 256 * 1024
"""256 KiB of UTF-8 text per agent turn: far above any model's output limit."""


def cap_reply_size(target: ChatTarget, max_bytes: int = DEFAULT_MAX_REPLY_BYTES) -> ChatTarget:
    """``target`` with each agent turn capped at ``max_bytes`` of UTF-8 text.

    Raises:
        ValueError: ``max_bytes`` is not positive.
    """
    if max_bytes < 1:
        raise ValueError(f"max_bytes must be >= 1, got {max_bytes}")
    return _CappedTarget(target, max_bytes)


class _CappedTarget:
    def __init__(self, inner: ChatTarget, max_bytes: int) -> None:
        self._inner = inner
        self._max_bytes = max_bytes

    async def open(self, ctx: SessionContext) -> ChatSession:
        return _CappedSession(await self._inner.open(ctx), self._max_bytes)


class _CappedSession:
    def __init__(self, inner: ChatSession, max_bytes: int) -> None:
        self._inner = inner
        self._max_bytes = max_bytes

    async def send(self, text: str) -> AsyncIterator[AgentChunk]:
        """Stream the inner reply until its text would pass the cap, then fail the turn."""
        left = self._max_bytes
        stream = self._inner.send(text)
        try:
            async for chunk in stream:
                data = chunk.text.encode("utf-8")
                if len(data) > left:
                    # Cut on a character boundary so the kept head is valid text.
                    head = data[:left].decode("utf-8", errors="ignore")
                    if head:
                        yield AgentChunk(text=head)
                    raise AgentError(
                        f"agent reply over {self._max_bytes} bytes; the transcript keeps the "
                        "first ones (raise it with --max-reply-bytes)"
                    )
                left -= len(data)
                yield chunk
        finally:
            # Release the inner stream (and its connection) however this one ends.
            if isinstance(stream, AsyncGenerator):
                await stream.aclose()

    async def close(self) -> SessionSummary:
        return await self._inner.close()
