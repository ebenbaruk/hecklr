"""Observed mode: read tool calls from OTLP/HTTP JSON trace exports.

Agents that keep their own backend emit OpenTelemetry GenAI ``execute_tool`` spans. A span joins a
trial through the first of these that is set:

1. span attribute ``hecklr.trial_id``;
2. span attribute ``http.request.header.x-hecklr-trial-id`` (a captured inbound header);
3. resource attribute ``hecklr.trial_id``;
4. the ``X-Hecklr-Trial-Id`` header of the export request itself.
"""

import base64
import json
from dataclasses import dataclass
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from hecklr_schemas.ids import new_id
from hecklr_world.session import ObservedToolCall

TRIAL_ID_ATTRIBUTE: Final = "hecklr.trial_id"
TRIAL_ID_HEADER_ATTRIBUTE: Final = "http.request.header.x-hecklr-trial-id"
_STATUS_ERROR: Final = (2, "STATUS_CODE_ERROR")


class _Otlp(BaseModel):
    # OTLP receivers must ignore fields they do not know (newer exporters add fields).
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class _KeyValue(_Otlp):
    key: str
    value: dict[str, JsonValue] = Field(default_factory=dict)


class _Status(_Otlp):
    code: int | str = 0
    message: str = ""


class _Span(_Otlp):
    span_id: str = Field(default="", alias="spanId")
    name: str = ""
    start_time_unix_nano: int = Field(default=0, alias="startTimeUnixNano")
    end_time_unix_nano: int = Field(default=0, alias="endTimeUnixNano")
    attributes: list[_KeyValue] = Field(default_factory=list)
    status: _Status = Field(default_factory=_Status)


class _ScopeSpans(_Otlp):
    spans: list[_Span] = Field(default_factory=list)


class _Resource(_Otlp):
    attributes: list[_KeyValue] = Field(default_factory=list)


class _ResourceSpans(_Otlp):
    resource: _Resource = Field(default_factory=_Resource)
    scope_spans: list[_ScopeSpans] = Field(default_factory=list, alias="scopeSpans")


class ExportTraceRequest(_Otlp):
    """The body of ``POST /v1/traces`` in the OTLP/HTTP JSON encoding."""

    resource_spans: list[_ResourceSpans] = Field(default_factory=list, alias="resourceSpans")


class _KeyValueList(_Otlp):
    values: list[_KeyValue] = Field(default_factory=list)


def any_value(value: dict[str, JsonValue]) -> JsonValue:
    """Decode an OTLP ``AnyValue`` (JSON encoding) into a plain JSON value."""
    if "stringValue" in value:
        return value["stringValue"]
    if "boolValue" in value:
        return bool(value["boolValue"])
    if "intValue" in value:
        # 64-bit integers are strings in OTLP JSON.
        return int(str(value["intValue"]))
    if "doubleValue" in value:
        return float(str(value["doubleValue"]))
    if "bytesValue" in value:
        return base64.b64decode(str(value["bytesValue"])).decode("utf-8", errors="replace")
    array = value.get("arrayValue")
    if isinstance(array, dict):
        items = array.get("values")
        values = items if isinstance(items, list) else []
        return [any_value(item) for item in values if isinstance(item, dict)]
    kvlist = value.get("kvlistValue")
    if isinstance(kvlist, dict):
        pairs = _KeyValueList.model_validate(kvlist).values
        return {pair.key: any_value(pair.value) for pair in pairs}
    return None


def _attributes(pairs: list[_KeyValue]) -> dict[str, JsonValue]:
    return {pair.key: any_value(pair.value) for pair in pairs}


def _maybe_json(value: JsonValue) -> JsonValue:
    # Semantic conventions allow structured values or JSON strings; accept both.
    if not isinstance(value, str):
        return value
    try:
        parsed: JsonValue = json.loads(value)
    except json.JSONDecodeError:
        return value
    return parsed


def _arguments(value: JsonValue) -> dict[str, JsonValue]:
    parsed = _maybe_json(value)
    if parsed is None:
        return {}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _trial_id(span: dict[str, JsonValue], resource: dict[str, JsonValue]) -> str | None:
    for candidate in (
        span.get(TRIAL_ID_ATTRIBUTE),
        span.get(TRIAL_ID_HEADER_ATTRIBUTE),
        resource.get(TRIAL_ID_ATTRIBUTE),
    ):
        if isinstance(candidate, list) and candidate:
            candidate = candidate[0]
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _tool_name(span: _Span, attributes: dict[str, JsonValue]) -> str:
    name = attributes.get("gen_ai.tool.name")
    # The convention names the span "execute_tool {gen_ai.tool.name}".
    return str(name) if name else span.name.removeprefix("execute_tool ")


def _error(span: _Span, attributes: dict[str, JsonValue]) -> str | None:
    error_type = attributes.get("error.type")
    if span.status.code in _STATUS_ERROR:
        return span.status.message or (str(error_type) if error_type else "error")
    return str(error_type) if error_type else None


@dataclass(frozen=True, slots=True)
class ObservedSpan:
    """A tool call span and the trial it belongs to (``None`` when no trial id was found)."""

    trial_id: str | None
    call: ObservedToolCall


def tool_spans(request: ExportTraceRequest, header_trial_id: str | None) -> list[ObservedSpan]:
    """Every ``execute_tool`` span of the export, with its trial id; other spans are skipped."""
    found: list[ObservedSpan] = []
    for resource_spans in request.resource_spans:
        resource = _attributes(resource_spans.resource.attributes)
        for scope in resource_spans.scope_spans:
            for span in scope.spans:
                attributes = _attributes(span.attributes)
                if attributes.get("gen_ai.operation.name") != "execute_tool":
                    continue
                call = ObservedToolCall(
                    tool=_tool_name(span, attributes),
                    call_id=str(attributes.get("gen_ai.tool.call.id") or span.span_id or new_id()),
                    args=_arguments(attributes.get("gen_ai.tool.call.arguments")),
                    result=_maybe_json(attributes.get("gen_ai.tool.call.result")),
                    error=_error(span, attributes),
                    start_unix_ns=span.start_time_unix_nano,
                    end_unix_ns=span.end_time_unix_nano,
                )
                trial_id = _trial_id(attributes, resource) or header_trial_id
                found.append(ObservedSpan(trial_id, call))
    return found
