"""YAML loading that remembers where every value came from.

Validation errors must name the file and line, and Pydantic only reports a path inside the parsed
data (``success.1.tool``). Composing the YAML node graph keeps each node's mark, so a path maps
back to a line. The loader also drops PyYAML's timestamp resolver: an ISO time such as
``2026-10-23T09:00`` stays a string, because schema fields hold JSON values, not ``datetime``.
"""

from collections.abc import Mapping
from pathlib import Path

import yaml
from pydantic import BaseModel, ValidationError

from hecklr_core.errors import ConfigError, ConfigIssue

_TIMESTAMP_TAG = "tag:yaml.org,2002:timestamp"

NodePath = tuple[str | int, ...]


class _Loader(yaml.SafeLoader):
    """Safe loader without implicit timestamps."""


_Loader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != _TIMESTAMP_TAG]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


class YamlDocument:
    """A parsed YAML file plus the line of every node, keyed by its path in the data."""

    def __init__(self, path: Path, data: object, lines: Mapping[NodePath, int]) -> None:
        self.path = path
        self.data = data
        self._lines = dict(lines)

    def line_of(self, loc: tuple[str | int, ...]) -> int | None:
        """Line of the deepest node along a Pydantic error location.

        Locations can contain segments that are not in the data (the tag of a discriminated
        union, ``function-after[...]``), so unmatched segments are skipped rather than ending
        the walk.
        """
        found: NodePath = ()
        for segment in loc:
            candidate = (*found, segment)
            if candidate in self._lines:
                found = candidate
        return self._lines.get(found)

    def validate[M: BaseModel](self, model: type[M]) -> M:
        """Validate the document as ``model``; errors become a ``ConfigError`` with lines."""
        try:
            return model.model_validate(self.data)
        except ValidationError as exc:
            raise ConfigError(
                [
                    ConfigIssue(
                        self.path,
                        self.line_of(tuple(error["loc"])),
                        _describe(error["loc"], error["msg"]),
                    )
                    for error in exc.errors()
                ]
            ) from exc


def _describe(loc: tuple[str | int, ...], message: str) -> str:
    where = ".".join(str(segment) for segment in loc)
    return f"{where}: {message}" if where else message


def load_yaml(path: Path) -> YamlDocument:
    """Parse one YAML file.

    Raises:
        ConfigError: the file is missing, is not valid YAML, holds several documents, or repeats
            a mapping key (PyYAML would silently keep the last one).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError([ConfigIssue(path, None, f"cannot read file: {exc.strerror}")]) from exc
    except UnicodeDecodeError as exc:
        raise ConfigError([ConfigIssue(path, None, f"not UTF-8 text: {exc.reason}")]) from exc
    try:
        return _parse(path, text)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        line = mark.line + 1 if mark is not None else None
        raise ConfigError([ConfigIssue(path, line, f"invalid YAML: {exc.problem}")]) from exc
    except yaml.YAMLError as exc:
        raise ConfigError([ConfigIssue(path, None, f"invalid YAML: {exc}")]) from exc


def _parse(path: Path, text: str) -> YamlDocument:
    loader = _Loader(text)
    try:
        node = loader.get_single_node()
        if node is None:
            raise ConfigError([ConfigIssue(path, None, "file is empty")])
        lines: dict[NodePath, int] = {}
        _index(path, node, (), lines)
        return YamlDocument(path, loader.construct_document(node), lines)
    finally:
        loader.dispose()


def _index(path: Path, node: yaml.Node, at: NodePath, lines: dict[NodePath, int]) -> None:
    lines[at] = node.start_mark.line + 1
    if isinstance(node, yaml.MappingNode):
        seen: dict[str, int] = {}
        for key_node, value_node in node.value:
            key = str(key_node.value)
            key_line = key_node.start_mark.line + 1
            if key in seen:
                raise ConfigError(
                    [
                        ConfigIssue(
                            path,
                            key_line,
                            f"duplicate key {key!r} (first on line {seen[key]})",
                        )
                    ]
                )
            seen[key] = key_line
            _index(path, value_node, (*at, key), lines)
            # Point a key's errors at the key itself, where the reader will look.
            lines[(*at, key)] = key_line
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _index(path, item, (*at, index), lines)


def load_model[M: BaseModel](path: Path, model: type[M]) -> M:
    """Parse ``path`` and validate it as ``model``, with file-and-line errors."""
    return load_yaml(path).validate(model)
