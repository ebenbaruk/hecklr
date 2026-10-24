"""A cap on how much text one agent turn may carry, around any ``ChatTarget`` (ADR 00hra).

An agent that floods (a streaming loop, a model that never stops, a debug dump in the reply)
would otherwise fill the result store and the HTML report: 20 MB replies made a 33-trial run
write 2.9 GB (issue #34). Past the cap the head of the reply is kept, so the transcript shows
what the agent sent, and the turn fails as an ``AgentError``.

The same wrapper refuses text that cannot be stored: a lone UTF-16 surrogate (what
``json.loads`` makes of an emoji escape cut in half) has no UTF-8 form, so it would fail later,
inside the harness, and the agent's fault would be retried and excluded as a harness error
(issue #112). The valid head is kept and the turn fails as an ``AgentError`` instead.
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
                unencodable: UnicodeEncodeError | None = None
                try:
                    data = chunk.text.encode("utf-8")
                except UnicodeEncodeError as exc:
                    # Only the valid head is kept, and it still has to fit the cap (issue #34):
                    # a flood ending in a lone surrogate must not be stored whole.
                    unencodable = exc
                    data = chunk.text[: exc.start].encode("utf-8")
                else:
                    _check_storable(chunk)
                if len(data) > left:
                    # Cut on a character boundary so the kept head is valid text.
                    head = data[:left].decode("utf-8", errors="ignore")
                    if head:
                        yield AgentChunk(text=head)
                    raise AgentError(
                        f"agent reply over {self._max_bytes} bytes; the transcript keeps the "
                        "first ones (raise it with --max-reply-bytes)"
                    )
                if unencodable is not None:
                    if data:
                        yield AgentChunk(text=data.decode("utf-8"))
                    bad = chunk.text[unencodable.start]
                    raise AgentError(_not_unicode(f"{bad!r} in its reply")) from unencodable
                left -= len(data)
                yield chunk
        finally:
            # Release the inner stream (and its connection) however this one ends.
            if isinstance(stream, AsyncGenerator):
                await stream.aclose()

    async def close(self) -> SessionSummary:
        return await self._inner.close()


def _check_storable(chunk: AgentChunk) -> None:
    """Fail the turn when a tool call, tool result or handoff holds text with no UTF-8 form."""
    if chunk.tool_call is None and chunk.tool_result is None and chunk.handoff is None:
        return
    try:
        chunk.model_dump_json()
    except ValueError as exc:  # pydantic's serialization error wraps the UnicodeEncodeError
        raise AgentError(_not_unicode(f"in a tool call, tool result or handoff ({exc})")) from exc


def _not_unicode(where: str) -> str:
    return f"agent sent text that is not valid Unicode (an unpaired surrogate) {where}"
