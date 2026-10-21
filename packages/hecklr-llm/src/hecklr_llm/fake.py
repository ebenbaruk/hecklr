"""``FakeLLM``: a deterministic provider for tests and the end-to-end fake pipeline."""

import json
import re
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal

from pydantic import BaseModel, JsonValue

from hecklr_llm.errors import FakeScriptExhaustedError
from hecklr_llm.keys import request_key
from hecklr_llm.structured import decode_json_reply
from hecklr_llm.tokens import estimate_input_tokens, estimate_tokens
from hecklr_schemas import LLMRequest, LLMResponse, LLMUsage

FakeReply = str | BaseModel | Mapping[str, JsonValue] | list[JsonValue]
"""A reply as tests like to write it; models and mappings are serialized to JSON."""

Responder = Callable[[LLMRequest], FakeReply]


class FakeLLM:
    """Deterministic ``LLMProvider``: the same request always gets the same reply.

    Replies are chosen in this order:

    1. ``rules``: the first ``(regex, reply)`` whose regex is found in the last message.
    2. ``responder``: a pure function of the request.
    3. ``script``: replies returned in call order; running out raises
       ``FakeScriptExhaustedError`` rather than silently inventing one.
    4. Otherwise a reply derived from the request's content hash. For JSON-schema requests it is
       the smallest object that satisfies the schema, so pipelines run end to end with no setup.

    Every request is recorded in ``calls``. Usage is estimated from text length; cost is zero.
    """

    def __init__(
        self,
        *,
        rules: Sequence[tuple[str, FakeReply]] = (),
        responder: Responder | None = None,
        script: Sequence[FakeReply] | None = None,
    ) -> None:
        self._rules = [(re.compile(pattern), reply) for pattern, reply in rules]
        self._responder = responder
        self._script = list(script) if script is not None else None
        self._script_position = 0
        self.calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Return the deterministic reply for ``request``."""
        self.calls.append(request)
        text = _render(self._choose(request))
        parsed = decode_json_reply(text) if request.json_schema is not None else None
        return LLMResponse(
            model=request.model,
            text=text,
            parsed=parsed,
            usage=LLMUsage(
                input_tokens=estimate_input_tokens(request), output_tokens=estimate_tokens(text)
            ),
            cost_usd=Decimal(0),
        )

    def _choose(self, request: LLMRequest) -> FakeReply:
        last_message = request.messages[-1].content
        for pattern, reply in self._rules:
            if pattern.search(last_message):
                return reply
        if self._responder is not None:
            return self._responder(request)
        if self._script is not None:
            return self._next_scripted(self._script)
        if request.json_schema is not None:
            return json.dumps(minimal_instance(request.json_schema))
        return f"fake reply {request_key(request)[:12]}"

    def _next_scripted(self, script: list[FakeReply]) -> FakeReply:
        if self._script_position >= len(script):
            raise FakeScriptExhaustedError(
                f"FakeLLM script has {len(script)} replies; "
                f"call {self._script_position + 1} has none"
            )
        reply = script[self._script_position]
        self._script_position += 1
        return reply


def _render(reply: FakeReply) -> str:
    if isinstance(reply, str):
        return reply
    if isinstance(reply, BaseModel):
        return reply.model_dump_json()
    return json.dumps(reply)


def minimal_instance(schema: Mapping[str, JsonValue]) -> JsonValue:
    """Smallest JSON value satisfying ``schema``: first enum value, required keys only.

    Covers the JSON Schema subset Pydantic emits (``$ref``/``$defs``, ``anyOf``, ``enum``,
    ``const``, typed objects, arrays and scalars).
    """
    return _instance(schema, root=schema)


def _instance(schema: Mapping[str, JsonValue], root: Mapping[str, JsonValue]) -> JsonValue:
    if "const" in schema:
        return schema["const"]
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    ref = schema.get("$ref")
    if isinstance(ref, str):
        return _instance(_resolve(ref, root), root)
    for combinator in ("anyOf", "oneOf", "allOf"):
        options = schema.get(combinator)
        if isinstance(options, list) and options and isinstance(options[0], dict):
            return _instance(options[0], root)
    return _typed_instance(schema, root)


def _typed_instance(schema: Mapping[str, JsonValue], root: Mapping[str, JsonValue]) -> JsonValue:
    kind = schema.get("type", "object")
    if isinstance(kind, list):
        kind = kind[0] if kind else "null"
    if kind == "object":
        properties = schema.get("properties")
        required = schema.get("required")
        if not isinstance(properties, dict) or not isinstance(required, list):
            return {}
        return {
            str(name): _instance(sub, root)
            for name in required
            if isinstance(sub := properties.get(str(name)), dict)
        }
    if kind == "array":
        items = schema.get("items")
        min_items = schema.get("minItems", 0)
        count = min_items if isinstance(min_items, int) else 0
        return [_instance(items, root) for _ in range(count)] if isinstance(items, dict) else []
    if kind == "string":
        min_length = schema.get("minLength", 0)
        return "x" * (min_length if isinstance(min_length, int) else 0)
    if kind in ("integer", "number"):
        return _smallest_number(schema)
    if kind == "boolean":
        return False
    return None


def _smallest_number(schema: Mapping[str, JsonValue]) -> int | float:
    minimum = schema.get("minimum")
    if isinstance(minimum, int | float):
        return minimum
    exclusive = schema.get("exclusiveMinimum")
    if isinstance(exclusive, int | float):
        return exclusive + 1
    maximum = schema.get("maximum")
    if isinstance(maximum, int | float) and maximum < 0:
        return maximum
    return 0


def _resolve(ref: str, root: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    if not ref.startswith("#/"):
        raise ValueError(f"only local $refs are supported, got {ref!r}")
    node: JsonValue = dict(root)
    for part in ref[2:].split("/"):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"unresolvable $ref {ref!r}")
        node = node[part]
    if not isinstance(node, dict):
        raise ValueError(f"$ref {ref!r} does not point at a schema object")
    return node
