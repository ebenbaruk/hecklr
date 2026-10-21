import pytest
from pydantic import BaseModel, ValidationError

from hecklr_schemas import Clock, FakeClock, MonotonicClock, RunId, is_ulid, new_id
from hecklr_schemas.ids import RunIdField


class _HasRunId(BaseModel):
    id: RunIdField


def test_new_id_returns_unique_canonical_ulids() -> None:
    ids = [new_id() for _ in range(1000)]

    assert len(set(ids)) == len(ids)
    assert all(is_ulid(value) for value in ids)


def test_new_ids_sort_by_creation_time_across_milliseconds() -> None:
    clock = MonotonicClock()
    first = new_id()
    while clock.now_ms() < 2:
        pass
    second = new_id()

    assert first < second


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-ulid",
        "01ARZ3NDEKTSV4RRFFQ69G5FA",
        "01arz3ndektsv4rrffq69g5fav",
        "01ARZ3NDEKTSV4RRFFQ69G5FAI",
    ],
)
def test_is_ulid_rejects_non_canonical_strings(value: str) -> None:
    assert not is_ulid(value)


def test_id_fields_validate_the_ulid_form() -> None:
    valid = new_id()

    assert _HasRunId(id=RunId(valid)).id == valid
    with pytest.raises(ValidationError, match="pattern"):
        _HasRunId(id=RunId("run-1"))


def test_fake_clock_advances_only_forward() -> None:
    clock = FakeClock(start_ms=100)

    assert clock.now_ms() == 100
    assert clock.advance(250) == 350
    assert clock.now_ms() == 350
    with pytest.raises(ValueError, match="backwards"):
        clock.advance(-1)


def test_fake_clock_rejects_negative_start() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        FakeClock(start_ms=-5)


def test_monotonic_clock_starts_near_zero_and_never_decreases() -> None:
    clock = MonotonicClock()
    readings = [clock.now_ms() for _ in range(1000)]

    assert 0 <= readings[0] < 50
    assert readings == sorted(readings)
    assert all(isinstance(value, int) for value in readings)


def test_both_clocks_satisfy_the_protocol() -> None:
    assert isinstance(MonotonicClock(), Clock)
    assert isinstance(FakeClock(), Clock)
