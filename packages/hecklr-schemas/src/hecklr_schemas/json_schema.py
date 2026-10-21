"""JSON Schema export for authored files, used by editors and by the loader's validation."""

import json
from pathlib import Path
from typing import Final

from pydantic import BaseModel

from hecklr_schemas.config import HecklrConfig
from hecklr_schemas.persona import Persona
from hecklr_schemas.scenario import Scenario
from hecklr_schemas.world import World

AUTHORING_MODELS: Final[dict[str, type[BaseModel]]] = {
    "hecklr-config": HecklrConfig,
    "scenario": Scenario,
    "persona": Persona,
    "world": World,
}
"""Schema name -> model for every YAML file a user writes."""

_SCHEMA_URI_BASE = "https://hecklr.dev/schemas"


def authoring_json_schemas() -> dict[str, dict[str, object]]:
    """Return the JSON Schema (draft 2020-12) of every authored file type, keyed by name."""
    schemas: dict[str, dict[str, object]] = {}
    for name, model in AUTHORING_MODELS.items():
        schema = model.model_json_schema(mode="validation")
        schemas[name] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": f"{_SCHEMA_URI_BASE}/{name}.schema.json",
            **schema,
        }
    return schemas


def write_json_schemas(directory: Path) -> list[Path]:
    """Write ``<name>.schema.json`` files into ``directory`` and return their paths."""
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for name, schema in authoring_json_schemas().items():
        path = directory / f"{name}.schema.json"
        path.write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(path)
    return written
