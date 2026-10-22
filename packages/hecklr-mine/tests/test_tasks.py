"""``map_concurrently``: ordered results, and a failure surfaces as itself, not as a group."""

import anyio
import pytest

from hecklr_mine.tasks import map_concurrently


def test_ws13_results_keep_input_order() -> None:
    async def slow_square(n: int) -> int:
        await anyio.sleep((5 - n) / 1000)
        return n * n

    assert anyio.run(lambda: map_concurrently(slow_square, range(5), concurrency=3)) == [
        0,
        1,
        4,
        9,
        16,
    ]


def test_ws13_a_failure_is_raised_as_itself_with_a_note_about_the_others() -> None:
    async def fail(n: int) -> int:
        raise KeyError(n)

    with pytest.raises(KeyError) as caught:
        anyio.run(lambda: map_concurrently(fail, [1, 2, 3], concurrency=3))
    assert not isinstance(caught.value, BaseExceptionGroup)
    assert caught.value.__notes__ == ["2 other concurrent call(s) failed too"]


def test_ws13_a_single_failure_has_no_note() -> None:
    async def fail_on_two(n: int) -> int:
        if n == 2:
            raise ValueError("two")
        return n

    with pytest.raises(ValueError, match="two") as caught:
        anyio.run(lambda: map_concurrently(fail_on_two, [1, 2, 3], concurrency=1))
    assert not hasattr(caught.value, "__notes__")
