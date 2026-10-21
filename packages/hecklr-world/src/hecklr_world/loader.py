"""Load a world file, reading every ``seed_file`` into inline records."""

import csv
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from hecklr_schemas.world import CollectionDef, Record, World
from hecklr_world.errors import WorldConfigError


def _read_seed_records(path: Path) -> list[Record]:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".csv":
        # CSV cells are strings; a record_schema that wants numbers should use a JSON or YAML seed.
        return [dict(row) for row in csv.DictReader(text.splitlines())]
    if suffix not in {".json", ".yaml", ".yml"}:
        raise WorldConfigError(f"{path}: seed files must be .json, .yaml, .yml or .csv")
    try:
        data: Any = json.loads(text) if suffix == ".json" else yaml.safe_load(text)
    except (json.JSONDecodeError, yaml.YAMLError) as exc:
        raise WorldConfigError(f"{path}: cannot parse seed file: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise WorldConfigError(f"{path}: a seed file must hold a list of records")
    return data


def _inline_seed(name: str, collection: CollectionDef, base: Path) -> CollectionDef:
    if collection.seed_file is None:
        return collection
    path = base / collection.seed_file
    if not path.is_file():
        raise WorldConfigError(f"collection {name!r}: seed file {path} does not exist")
    records = [*collection.records, *_read_seed_records(path)]
    try:
        return CollectionDef.model_validate(
            {**collection.model_dump(), "records": records, "seed_file": None}
        )
    except ValidationError as exc:
        raise WorldConfigError(f"collection {name!r}: invalid seed data in {path}: {exc}") from exc


def load_world(path: Path) -> World:
    """Parse a world YAML file and return it with seed files inlined (relative to the file).

    The result is self-contained: the sandbox needs no file system access to load a trial.
    """
    try:
        world = World.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (yaml.YAMLError, ValidationError) as exc:
        raise WorldConfigError(f"{path}: invalid world: {exc}") from exc
    collections = {
        name: _inline_seed(name, collection, path.parent)
        for name, collection in world.collections.items()
    }
    return world.model_copy(update={"collections": collections})
