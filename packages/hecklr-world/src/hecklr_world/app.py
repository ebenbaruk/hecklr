"""The shared world server: every trial's tools under ``/t/{trial_id}``, plus the OTLP receiver.

One process and one port serve all concurrent trials, so a run at concurrency 32+ never juggles
32 ports. Routes:

- ``GET  /t/{trial_id}/openapi.json``: the trial's OpenAPI 3.1 document, one operation per tool.
- ``POST /t/{trial_id}/tools/{tool}``: call a tool with a JSON object body.
- ``/t/{trial_id}/mcp``: the same tools over MCP (streamable HTTP, official SDK, stateless).
- ``POST /v1/traces``: OTLP/HTTP JSON traces for observed mode.
"""

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any, Final

import mcp.types as mcp_types
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server import Server, ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from pydantic import JsonValue, ValidationError
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from hecklr_schemas.adapters import TRIAL_ID_HEADER
from hecklr_schemas.world import World
from hecklr_world.errors import InvalidArgumentsError, ToolError
from hecklr_world.otlp import ExportTraceRequest, tool_spans
from hecklr_world.session import SessionClosedError, TrialWorld

SERVER_NAME: Final = "hecklr-world"

Sessions = Mapping[str, TrialWorld]
"""Open trial worlds by trial id; owned and mutated by the ``Sandbox``."""

_ERROR_SCHEMA: Final[dict[str, JsonValue]] = {
    "type": "object",
    "required": ["error"],
    "properties": {
        "error": {
            "type": "object",
            "required": ["code", "message"],
            "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
        }
    },
}


def openapi_document(world: World, server_url: str) -> dict[str, JsonValue]:
    """An OpenAPI 3.1 document for ``world``'s tools served at ``server_url``.

    OpenAPI 3.1 schemas are JSON Schema 2020-12, so each tool's ``params`` is used as is.
    """
    paths: dict[str, JsonValue] = {}
    for tool in world.tools:
        paths[f"/tools/{tool.name}"] = {
            "post": {
                "operationId": tool.name,
                "summary": tool.description.splitlines()[0] if tool.description else tool.name,
                "description": tool.description,
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": tool.params}},
                },
                "responses": {
                    "200": {
                        "description": "The tool result.",
                        "content": {"application/json": {"schema": {}}},
                    },
                    "default": {
                        "description": "The call failed.",
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
                        },
                    },
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {"title": world.id, "version": "1", "description": world.description or ""},
        "servers": [{"url": server_url}],
        "paths": paths,
        "components": {"schemas": {"Error": _ERROR_SCHEMA}},
    }


def _not_found(message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": "not_found", "message": message}}, status_code=404)


def _served(sessions: Sessions, trial_id: str) -> TrialWorld | None:
    session = sessions.get(trial_id)
    return None if session is None or session.observed else session


async def _json_object(request: Request) -> dict[str, JsonValue]:
    raw = await request.body()
    if not raw.strip():
        return {}
    try:
        body: JsonValue = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise InvalidArgumentsError(f"request body is not JSON: {exc.msg}") from exc
    if not isinstance(body, dict):
        raise InvalidArgumentsError("the request body must be a JSON object of tool arguments")
    return body


def _mcp_server(sessions: Sessions) -> Server[Any]:
    def session_for(ctx: ServerRequestContext[Any, Any]) -> TrialWorld:
        trial_id = ctx.request.path_params.get("trial_id") if ctx.request is not None else None
        session = _served(sessions, trial_id) if isinstance(trial_id, str) else None
        if session is None:
            raise MCPError(mcp_types.INVALID_PARAMS, f"no open trial world {trial_id!r}")
        return session

    async def list_tools(
        ctx: ServerRequestContext[Any, Any], params: mcp_types.PaginatedRequestParams | None
    ) -> mcp_types.ListToolsResult:
        tools = [
            mcp_types.Tool(name=tool.name, description=tool.description, input_schema=tool.params)
            for tool in session_for(ctx).tools
        ]
        return mcp_types.ListToolsResult(tools=tools)

    async def call_tool(
        ctx: ServerRequestContext[Any, Any], params: mcp_types.CallToolRequestParams
    ) -> mcp_types.CallToolResult:
        session = session_for(ctx)
        try:
            outcome = await session.call_tool(params.name, params.arguments or {})
        except SessionClosedError as exc:
            raise MCPError(mcp_types.INVALID_PARAMS, str(exc)) from exc
        body = outcome.body()
        return mcp_types.CallToolResult(
            content=[mcp_types.TextContent(type="text", text=json.dumps(body))],
            # MCP structured content must be an object; wrap so lists and scalars fit.
            structured_content={"result": body} if outcome.ok else None,
            is_error=not outcome.ok,
        )

    return Server(SERVER_NAME, on_list_tools=list_tools, on_call_tool=call_tool)


class _McpEndpoint:
    """The MCP session manager as an ASGI endpoint; Starlette passes class instances raw ASGI."""

    def __init__(self, manager: StreamableHTTPSessionManager) -> None:
        self._manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self._manager.handle_request(scope, receive, send)


def create_app(sessions: Sessions, *, allowed_hosts: list[str]) -> FastAPI:
    """Build the world server for the sessions the sandbox registers.

    ``allowed_hosts`` feeds the MCP SDK's DNS-rebinding protection (``host:port`` patterns).
    """
    manager = StreamableHTTPSessionManager(
        app=_mcp_server(sessions),
        # Stateless + JSON: every MCP POST is self-contained, so many trials and reconnecting
        # agents never share or leak session state through the SDK.
        stateless=True,
        json_response=True,
        security_settings=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=[f"http://{host}" for host in allowed_hosts],
        ),
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with manager.run():
            yield

    app = FastAPI(title=SERVER_NAME, lifespan=lifespan, docs_url=None, redoc_url=None)

    # A Route (not a Mount) so the exact URL answers: a Mount would 307-redirect ``/mcp`` to
    # ``/mcp/``, which not every MCP client follows for a POST.
    app.router.routes.append(Route("/t/{trial_id}/mcp", endpoint=_McpEndpoint(manager)))

    @app.get("/t/{trial_id}/openapi.json")
    async def trial_openapi(trial_id: str, request: Request) -> JSONResponse:
        session = _served(sessions, trial_id)
        if session is None:
            return _not_found(f"no open trial world {trial_id!r}")
        server_url = str(request.url_for("trial_openapi", trial_id=trial_id)).removesuffix(
            "/openapi.json"
        )
        return JSONResponse(openapi_document(session.world, server_url))

    @app.post("/t/{trial_id}/tools/{tool}")
    async def call_tool(trial_id: str, tool: str, request: Request) -> JSONResponse:
        session = _served(sessions, trial_id)
        if session is None:
            return _not_found(f"no open trial world {trial_id!r}")
        try:
            args = await _json_object(request)
        except ToolError as exc:
            return JSONResponse(exc.to_json(), status_code=exc.http_status)
        try:
            outcome = await session.call_tool(tool, args)
        except SessionClosedError as exc:
            return _not_found(str(exc))
        return JSONResponse(outcome.body(), status_code=outcome.http_status)

    @app.post("/v1/traces")
    async def receive_traces(request: Request) -> JSONResponse:
        if not request.headers.get("content-type", "").startswith("application/json"):
            return JSONResponse(
                {"error": {"code": "unsupported_media_type", "message": "send OTLP/HTTP JSON"}},
                status_code=415,
            )
        try:
            export = ExportTraceRequest.model_validate_json(await request.body())
        except ValidationError as exc:
            return JSONResponse(
                {"error": {"code": "invalid_arguments", "message": str(exc)}}, status_code=400
            )
        rejected = 0
        for span in tool_spans(export, request.headers.get(TRIAL_ID_HEADER)):
            session = sessions.get(span.trial_id) if span.trial_id else None
            if session is None or not session.observed or session.closed:
                rejected += 1
                continue
            session.record_observed(span.call)
        if not rejected:
            return JSONResponse({})
        message = f"{rejected} tool span(s) matched no open observed-mode trial"
        # OTLP JSON encodes int64 fields as strings.
        partial = {"rejectedSpans": str(rejected), "errorMessage": message}
        return JSONResponse({"partialSuccess": partial})

    return app
