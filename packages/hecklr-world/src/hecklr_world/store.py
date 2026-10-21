"""The per-trial in-memory store: collection -> record key -> record.

Every read returns a deep copy and every write stores one, so neither the agent's result payloads
nor a Python tool holding a reference can change state behind the store's back. That, plus one
fresh store per trial built from the immutable ``World``, is what keeps concurrent trials isolated.
"""

import copy
import json
from collections.abc import Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue

from hecklr_schemas.world import CollectionDef, Record, World
from hecklr_world.errors import (
    ConflictError,
    InvalidArgumentsError,
    NotFoundError,
    WorldConfigError,
)


def record_key(value: JsonValue) -> str:
    """The string form of a key value; snapshots and state expressions index with it.

    Non-string keys use their JSON text (``7`` -> ``"7"``, ``True`` -> ``"true"``), which is what
    an agent sends when it passes the key back as a string.
    """
    return value if isinstance(value, str) else json.dumps(value)


class _Collection:
    def __init__(self, name: str, definition: CollectionDef) -> None:
        if definition.seed_file is not None:
            raise WorldConfigError(
                f"collection {name!r} still references seed_file {definition.seed_file!r}: "
                "load the world with hecklr_world.load_world() so seed files are read"
            )
        self.name = name
        self.key_field = definition.key
        self.validator = _record_validator(name, definition)
        self.records: dict[str, Record] = {}
        for record in definition.records:
            key = record_key(record[self.key_field])
            if key in self.records:
                raise WorldConfigError(f"collection {name!r}: duplicate seed record key {key!r}")
            self.check_record(record, on_error=WorldConfigError)
            self.records[key] = copy.deepcopy(record)

    def check_record(
        self, record: Record, *, on_error: type[Exception] = InvalidArgumentsError
    ) -> None:
        if self.validator is None:
            return
        error = next(iter(sorted(self.validator.iter_errors(record), key=str)), None)
        if error is not None:
            raise on_error(f"{self.name} record is invalid: {error.message}")

    def require(self, key: str) -> Record:
        try:
            return self.records[key]
        except KeyError:
            raise NotFoundError(f"no {self.name} record with {self.key_field}={key!r}") from None

    def next_key(self) -> str:
        # Deterministic, so two runs of the same trial create identically keyed records.
        index = len(self.records) + 1
        while f"{self.name}-{index}" in self.records:
            index += 1
        return f"{self.name}-{index}"


def _record_validator(name: str, definition: CollectionDef) -> Draft202012Validator | None:
    if definition.record_schema is None:
        return None
    try:
        Draft202012Validator.check_schema(definition.record_schema)
    except SchemaError as exc:
        raise WorldConfigError(
            f"collection {name!r}: invalid record_schema: {exc.message}"
        ) from exc
    return Draft202012Validator(definition.record_schema)


def _matches(record: Record, filters: Mapping[str, JsonValue]) -> bool:
    return all(field in record and record[field] == value for field, value in filters.items())


class Store:
    """One trial's world state, loaded fresh from a ``World``.

    Python tools receive the store as their first argument and use these methods; they raise
    ``ToolError`` subclasses that reach the agent as structured errors.
    """

    def __init__(self, world: World) -> None:
        self.world_id = world.id
        self._collections = {
            name: _Collection(name, definition) for name, definition in world.collections.items()
        }

    def _collection(self, name: str) -> _Collection:
        try:
            return self._collections[name]
        except KeyError:
            raise WorldConfigError(f"world {self.world_id!r} has no collection {name!r}") from None

    def key_field(self, collection: str) -> str:
        """The field that identifies records of ``collection``."""
        return self._collection(collection).key_field

    def get(self, collection: str, key: JsonValue) -> Record:
        """The record with this key, or ``NotFoundError``."""
        return copy.deepcopy(self._collection(collection).require(record_key(key)))

    def list(self, collection: str, filters: Mapping[str, JsonValue] | None = None) -> list[Record]:
        """Every record whose fields equal ``filters``, in insertion order."""
        records = self._collection(collection).records.values()
        return [copy.deepcopy(r) for r in records if _matches(r, filters or {})]

    def find(self, collection: str, filters: Mapping[str, JsonValue]) -> Record:
        """The first record whose fields equal ``filters``, or ``NotFoundError``."""
        coll = self._collection(collection)
        for record in coll.records.values():
            if _matches(record, filters):
                return copy.deepcopy(record)
        criteria = ", ".join(f"{field}={value!r}" for field, value in filters.items())
        raise NotFoundError(f"no {collection} record matches {criteria or 'the query'}")

    def create(self, collection: str, record: Mapping[str, JsonValue]) -> Record:
        """Insert ``record``, generating its key if absent; ``ConflictError`` if the key exists."""
        coll = self._collection(collection)
        new = copy.deepcopy(dict(record))
        if coll.key_field not in new:
            new[coll.key_field] = coll.next_key()
        key = record_key(new[coll.key_field])
        if key in coll.records:
            raise ConflictError(f"{collection} record {coll.key_field}={key!r} already exists")
        coll.check_record(new)
        coll.records[key] = new
        return copy.deepcopy(new)

    def update(self, collection: str, key: JsonValue, fields: Mapping[str, JsonValue]) -> Record:
        """Set ``fields`` on the record with this key and return the updated record."""
        coll = self._collection(collection)
        current = coll.require(record_key(key))
        updated = {**current, **copy.deepcopy(dict(fields))}
        if record_key(updated[coll.key_field]) != record_key(key):
            raise InvalidArgumentsError(f"cannot change the key field {coll.key_field!r}")
        coll.check_record(updated)
        coll.records[record_key(key)] = updated
        return copy.deepcopy(updated)

    def delete(self, collection: str, key: JsonValue) -> Record:
        """Remove the record with this key and return it."""
        coll = self._collection(collection)
        coll.require(record_key(key))
        return coll.records.pop(record_key(key))

    def state(self) -> dict[str, dict[str, Record]]:
        """A deep copy of every collection, in the ``WorldSnapshot.collections`` shape."""
        return {name: copy.deepcopy(coll.records) for name, coll in self._collections.items()}
