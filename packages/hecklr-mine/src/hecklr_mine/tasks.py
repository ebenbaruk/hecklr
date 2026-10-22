"""Bounded concurrency for the per-conversation stages (LLM scrub, judges, summaries)."""

from collections.abc import Awaitable, Callable, Sequence

import anyio


async def map_concurrently[T, R](
    fn: Callable[[T], Awaitable[R]], items: Sequence[T], *, concurrency: int
) -> list[R]:
    """``[await fn(item) for item in items]``, at most ``concurrency`` at a time, in input order.

    The first failure cancels the remaining calls and is raised as itself, not wrapped in the
    task group's ``ExceptionGroup``: callers map typed errors (``BudgetExceededError``,
    ``LLMError``) to exit codes, and an ``except`` clause never matches a group.
    """
    results: dict[int, R] = {}
    limiter = anyio.CapacityLimiter(concurrency)

    async def run(index: int, item: T) -> None:
        async with limiter:
            results[index] = await fn(item)

    try:
        async with anyio.create_task_group() as group:
            for index, item in enumerate(items):
                group.start_soon(run, index, item)
    except BaseExceptionGroup as errors:
        leaves = _leaves(errors)
        first = leaves[0]
        if len(leaves) > 1:
            first.add_note(f"{len(leaves) - 1} other concurrent call(s) failed too")
        raise first from None
    return [results[index] for index in range(len(items))]


def _leaves(group: BaseExceptionGroup[BaseException]) -> list[BaseException]:
    leaves: list[BaseException] = []
    for error in group.exceptions:
        if isinstance(error, BaseExceptionGroup):
            leaves.extend(_leaves(error))
        else:
            leaves.append(error)
    return leaves
