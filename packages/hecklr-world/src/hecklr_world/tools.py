"""Tool execution: argument validation and the operations of ADR 0004."""

import importlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import JsonValue

from hecklr_schemas.world import CollectionOp, PythonOp, ToolDef, World
from hecklr_world.errors import InvalidArgumentsError, UnknownToolError, WorldConfigError
from hecklr_world.store import Store

PythonTool = Callable[..., JsonValue]
"""A Python tool: ``fn(world: Store, **params) -> JSON result``."""


def resolve_entrypoint(entrypoint: str) -> PythonTool:
    """Import ``module.path:function`` and return the function.

    Only importlib is used (never ``eval``); the module is the user's own tool code, loaded the
    same way a ``python`` adapter or grader entry point is.
    """
    module_name, _, attribute = entrypoint.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise WorldConfigError(f"cannot import tool module {module_name!r}: {exc}") from exc
    function = getattr(module, attribute, None)
    if not callable(function):
        raise WorldConfigError(f"tool entry point {entrypoint!r} is not a callable")
    tool: PythonTool = function
    return tool


Operation = Callable[[Store, Mapping[str, JsonValue]], JsonValue]


@dataclass(frozen=True, slots=True)
class _Tool:
    definition: ToolDef
    validator: Draft202012Validator
    run: Operation


def _compile(tool: ToolDef) -> _Tool:
    try:
        Draft202012Validator.check_schema(tool.params)
    except SchemaError as exc:
        raise WorldConfigError(f"tool {tool.name!r}: invalid params schema: {exc.message}") from exc
    return _Tool(tool, Draft202012Validator(tool.params), _operation(tool.op))


def _operation(op: CollectionOp | PythonOp) -> Operation:
    if isinstance(op, PythonOp):
        function = resolve_entrypoint(op.entrypoint)
        return lambda store, args: function(store, **args)
    return lambda store, args: _run_collection_op(store, op, args)


class Toolbox:
    """A world's tools, validated and with Python entry points resolved, ready to execute."""

    def __init__(self, world: World) -> None:
        self._tools = {tool.name: _compile(tool) for tool in world.tools}

    @property
    def definitions(self) -> list[ToolDef]:
        """The tool definitions, in world order."""
        return [tool.definition for tool in self._tools.values()]

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def execute(self, store: Store, name: str, args: Mapping[str, JsonValue]) -> JsonValue:
        """Validate ``args`` against the tool's schema and run its operation on ``store``."""
        tool = self._tools.get(name)
        if tool is None:
            raise UnknownToolError(f"unknown tool {name!r}")
        error = next(iter(sorted(tool.validator.iter_errors(dict(args)), key=str)), None)
        if error is not None:
            where = "/".join(str(part) for part in error.absolute_path)
            raise InvalidArgumentsError(f"{name}: {where + ': ' if where else ''}{error.message}")
        return tool.run(store, args)


def _run_collection_op(store: Store, op: CollectionOp, args: Mapping[str, JsonValue]) -> JsonValue:
    if op.kind == "list":
        return list(store.list(op.collection, args))
    if op.kind == "find":
        return store.find(op.collection, args)
    if op.kind == "create":
        return store.create(op.collection, args)
    key_param = op.key_param or store.key_field(op.collection)
    if key_param not in args:
        raise InvalidArgumentsError(f"missing required argument {key_param!r}")
    key = args[key_param]
    if op.kind == "get":
        return store.get(op.collection, key)
    if op.kind == "delete":
        return store.delete(op.collection, key)
    fields = {name: value for name, value in args.items() if name != key_param}
    return store.update(op.collection, key, fields)
