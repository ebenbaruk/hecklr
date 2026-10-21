"""The store, snapshots, diffs and world loading."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from hecklr_schemas import TrialBrief, World, WorldSnapshot
from hecklr_world import (
    ConflictError,
    InvalidArgumentsError,
    NotFoundError,
    Store,
    TrialWorld,
    WorldChange,
    WorldConfigError,
    diff_snapshots,
    load_world,
)

BriefFactory = Callable[..., TrialBrief]
SinkFactory = Callable[..., Any]


def _world(**collections: Any) -> World:
    return World.model_validate({"id": "w", "collections": collections})


def test_reads_return_copies_so_callers_cannot_mutate_state(example_world: World) -> None:
    store = Store(example_world)
    record = store.get("customers", "C-1042")
    record["name"] = "Mallory"
    store.list("customers")[0]["name"] = "Mallory"
    assert store.get("customers", "C-1042")["name"] == "Camille Durand"


def test_each_store_starts_from_the_seed_and_never_touches_the_world(example_world: World) -> None:
    first = Store(example_world)
    first.update("appointments", "A-1", {"slot": "2026-12-01T09:00"})
    second = Store(example_world)
    assert second.get("appointments", "A-1")["slot"] == "2026-10-23T09:00"
    assert example_world.collections["appointments"].records[0]["slot"] == "2026-10-23T09:00"


def test_create_refuses_duplicate_keys() -> None:
    store = Store(_world(items={"key": "sku", "records": [{"sku": "X"}]}))
    with pytest.raises(ConflictError, match="items record sku='X' already exists"):
        store.create("items", {"sku": "X"})


def test_generated_keys_skip_taken_ones() -> None:
    store = Store(_world(items={"records": [{"id": "items-2"}]}))
    assert store.create("items", {})["id"] == "items-3"
    assert store.create("items", {})["id"] == "items-4"


def test_update_cannot_change_the_key_field() -> None:
    store = Store(_world(items={"records": [{"id": "a"}]}))
    with pytest.raises(InvalidArgumentsError, match="cannot change the key field 'id'"):
        store.update("items", "a", {"id": "b"})


def test_integer_keys_are_indexed_by_their_string_form() -> None:
    store = Store(_world(orders={"key": "n", "records": [{"n": 1, "total": 5}]}))
    assert store.get("orders", 1) == {"n": 1, "total": 5}
    assert store.state() == {"orders": {"1": {"n": 1, "total": 5}}}


def test_find_without_a_match_says_what_it_searched_for() -> None:
    store = Store(_world(items={"records": [{"id": "a"}]}, empty={}))
    with pytest.raises(NotFoundError, match="no empty record matches the query"):
        store.find("empty", {})
    with pytest.raises(NotFoundError, match="no items record matches id='b'"):
        store.find("items", {"id": "b"})
    assert store.find("items", {"id": "a"}) == {"id": "a"}


@pytest.mark.parametrize(
    ("collections", "message"),
    [
        ({"items": {"records": [{"id": "a"}, {"id": "a"}]}}, "duplicate seed record key 'a'"),
        ({"items": {"seed_file": "items.json"}}, "still references seed_file 'items.json'"),
        (
            {"items": {"record_schema": {"type": 7}}},
            "collection 'items': invalid record_schema",
        ),
        (
            {"items": {"record_schema": {"required": ["n"]}, "records": [{"id": "a"}]}},
            "items record is invalid: 'n' is a required property",
        ),
    ],
)
def test_bad_worlds_fail_loudly(collections: dict[str, Any], message: str) -> None:
    with pytest.raises(WorldConfigError, match=message):
        Store(_world(**collections))


def test_python_tools_referencing_unknown_collections_are_config_errors() -> None:
    store = Store(_world(items={}))
    with pytest.raises(WorldConfigError, match="world 'w' has no collection 'nope'"):
        store.get("nope", "x")


@pytest.mark.anyio
async def test_snapshot_is_stamped_with_the_sink_clock_and_detached(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief()
    sink = make_sink(brief.trial_id)
    trial = TrialWorld(brief, example_world, sink)
    before = trial.snapshot()
    sink.clock.advance(2_500)
    await trial.call_tool(
        "reschedule_appointment", {"appointment_id": "A-1", "slot": "2026-10-28T09:00"}
    )
    after = trial.snapshot()
    assert (before.world_id, before.taken_at_ms, after.taken_at_ms) == (
        "example-maintenance",
        0,
        2_500,
    )
    assert before.collections["appointments"]["A-1"]["slot"] == "2026-10-23T09:00"
    assert after.collections["appointments"]["A-1"]["slot"] == "2026-10-28T09:00"
    assert after.collections["policies"]["cancellation"]["policy_id"] == "cancellation"


def _snapshot(collections: dict[str, dict[str, dict[str, Any]]]) -> WorldSnapshot:
    return WorldSnapshot(world_id="w", taken_at_ms=0, collections=collections)


def test_diff_lists_added_removed_and_changed_paths_in_a_stable_order() -> None:
    before = _snapshot(
        {
            "appointments": {
                "A-1": {
                    "slot": "2026-10-23T09:00",
                    "notes": {"door": "blue", "dog": True},
                    "tech": "Lea",
                },
                "A-2": {"slot": "2026-11-02T14:00"},
            },
            "old": {"x": {"id": "x"}},
        }
    )
    after = _snapshot(
        {
            "appointments": {
                "A-1": {
                    "slot": "2026-10-28T09:00",
                    "notes": {"door": "red", "dog": True},
                    "status": "moved",
                },
                "A-3": {"slot": "2026-12-01T10:00"},
            },
            "refunds": {"refunds-1": {"amount-eur": 40}},
        }
    )
    assert diff_snapshots(before, after) == [
        WorldChange(
            path="appointments['A-1'].notes.door", kind="changed", before="blue", after="red"
        ),
        WorldChange(
            path="appointments['A-1'].slot",
            kind="changed",
            before="2026-10-23T09:00",
            after="2026-10-28T09:00",
        ),
        WorldChange(path="appointments['A-1'].status", kind="added", after="moved"),
        WorldChange(path="appointments['A-1'].tech", kind="removed", before="Lea"),
        WorldChange(
            path="appointments['A-2']", kind="removed", before={"slot": "2026-11-02T14:00"}
        ),
        WorldChange(path="appointments['A-3']", kind="added", after={"slot": "2026-12-01T10:00"}),
        WorldChange(path="old['x']", kind="removed", before={"id": "x"}),
        WorldChange(path="refunds['refunds-1']", kind="added", after={"amount-eur": 40}),
    ]


def test_diff_of_identical_snapshots_is_empty_and_quotes_odd_field_names() -> None:
    snap = _snapshot({"c": {"k": {"a b": 1}}})
    assert diff_snapshots(snap, snap) == []
    changed = _snapshot({"c": {"k": {"a b": 2}}})
    assert diff_snapshots(snap, changed)[0].path == "c['k']['a b']"


def test_load_world_inlines_json_yaml_and_csv_seed_files(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text('[{"id": "j1"}]', encoding="utf-8")
    (tmp_path / "b.yml").write_text("- {id: y1}\n", encoding="utf-8")
    (tmp_path / "c.csv").write_text("id,name\nc1,Ann\n", encoding="utf-8")
    (tmp_path / "w.yaml").write_text(
        "id: w\ncollections:\n"
        "  a: {seed_file: a.json, records: [{id: inline}]}\n"
        "  b: {seed_file: b.yml}\n"
        "  c: {seed_file: c.csv}\n",
        encoding="utf-8",
    )
    world = load_world(tmp_path / "w.yaml")
    assert world.collections["a"].records == [{"id": "inline"}, {"id": "j1"}]
    assert world.collections["b"].records == [{"id": "y1"}]
    assert world.collections["c"].records == [{"id": "c1", "name": "Ann"}]
    assert all(c.seed_file is None for c in world.collections.values())


@pytest.mark.parametrize(
    ("files", "message"),
    [
        ({"w.yaml": "id: w\ncollections: {a: {seed_file: gone.json}}"}, "gone.json does not exist"),
        ({"w.yaml": "id: w\ncollections: {a: {seed_file: a.txt}}", "a.txt": ""}, "must be .json"),
        ({"w.yaml": "id: w\ncollections: {a: {seed_file: a.json}}", "a.json": "{"}, "cannot parse"),
        (
            {"w.yaml": "id: w\ncollections: {a: {seed_file: a.json}}", "a.json": '{"id": 1}'},
            "must hold a list of records",
        ),
        (
            {"w.yaml": "id: w\ncollections: {a: {seed_file: a.json}}", "a.json": '[{"x": 1}]'},
            "invalid seed data",
        ),
        ({"w.yaml": "id: w\ncollections: [1, 2]"}, "invalid world"),
        ({"w.yaml": "id: w\n  bad: : yaml"}, "invalid world"),
    ],
)
def test_load_world_reports_bad_files(tmp_path: Path, files: dict[str, str], message: str) -> None:
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    with pytest.raises(WorldConfigError, match=message):
        load_world(tmp_path / "w.yaml")
