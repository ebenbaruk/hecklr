"""Request templates and response paths: how a config describes an agent's JSON message schema.

A template is any JSON value whose strings may hold ``{{name}}`` placeholders; rendering walks the
structure and substitutes inside strings, so a message with quotes or newlines can never break the
JSON. A path is dotted keys and list indexes, e.g. ``choices.0.message.content``.
"""

import re
from collections.abc import Mapping, Set

from pydantic import JsonValue

from hecklr_adapters.errors import AdapterConfigError, AgentError

PLACEHOLDERS: frozenset[str] = frozenset(
    {"message", "session_id", "trial_id", "world_url", "world_mcp_url"}
)
"""Names a request template may use; ``context.template_values`` gives their values."""

_PLACEHOLDER = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def placeholders(template: JsonValue) -> set[str]:
    """Every placeholder name used anywhere in ``template``."""
    if isinstance(template, str):
        return set(_PLACEHOLDER.findall(template))
    if isinstance(template, dict):
        return set().union(*(placeholders(value) for value in template.values()))
    if isinstance(template, list):
        return set().union(*(placeholders(item) for item in template))
    return set()


def check_template(template: JsonValue, *, allowed: Set[str] = PLACEHOLDERS) -> None:
    """Reject a template that uses unknown placeholders or never sends the message."""
    used = placeholders(template)
    if unknown := used - allowed:
        raise AdapterConfigError(
            f"unknown template placeholder(s) {sorted(unknown)}; allowed: {sorted(allowed)}"
        )
    if "message" not in used:
        raise AdapterConfigError("the request template must contain {{message}}")


def render(template: JsonValue, values: Mapping[str, str]) -> JsonValue:
    """Return a copy of ``template`` with every ``{{name}}`` replaced by ``values[name]``."""
    if isinstance(template, str):
        return _PLACEHOLDER.sub(lambda match: values[match.group(1)], template)
    if isinstance(template, dict):
        return {key: render(value, values) for key, value in template.items()}
    if isinstance(template, list):
        return [render(item, values) for item in template]
    return template


def check_path(path: str) -> None:
    """Reject a path with empty segments, e.g. ``""`` or ``"a..b"``."""
    if not all(path.split(".")):
        raise AdapterConfigError(f"invalid response path {path!r}: empty segment")


def extract(data: JsonValue, path: str) -> JsonValue:
    """Follow ``path`` into ``data``; a missing step is the agent's fault."""
    node = data
    for step in path.split("."):
        if isinstance(node, dict) and step in node:
            node = node[step]
        elif isinstance(node, list) and step.isdigit() and int(step) < len(node):
            node = node[int(step)]
        else:
            raise AgentError(f"agent reply has no {path!r} (stopped at {step!r})")
    return node


def find(data: JsonValue, path: str) -> JsonValue:
    """Like ``extract``, but a missing path reads as ``None`` (for optional fields)."""
    try:
        return extract(data, path)
    except AgentError:
        return None


def extract_text(data: JsonValue, path: str) -> str:
    """Follow ``path`` into ``data`` and require a string there."""
    value = extract(data, path)
    if not isinstance(value, str):
        raise AgentError(f"agent reply at {path!r} is {type(value).__name__}, not text")
    return value
