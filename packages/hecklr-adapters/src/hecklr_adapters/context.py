"""What a chat adapter tells the agent about its trial: the correlation id and the world sandbox.

An agent under test must call its own trial's world sandbox, or the trial's state assertions
grade a world the agent never touched. The sandbox URLs differ per trial (and the sandbox port
per run), so every request carries them (ADR 0007d):

- headers: ``X-Hecklr-Trial-Id`` always; ``X-Hecklr-World-Url`` (the OpenAPI base, tools at
  ``POST <url>/tools/<name>``) and ``X-Hecklr-World-Mcp-Url`` when the trial has a sandbox;
- request template placeholders ``{{world_url}}`` and ``{{world_mcp_url}}``, empty when the trial
  has no sandbox (observed tool mode).
"""

from typing import Final

from hecklr_schemas import SessionContext

WORLD_URL_HEADER: Final = "X-Hecklr-World-Url"
"""Base URL of the trial's world sandbox OpenAPI routes."""
WORLD_MCP_URL_HEADER: Final = "X-Hecklr-World-Mcp-Url"
"""MCP endpoint of the trial's world sandbox."""


def session_headers(ctx: SessionContext) -> dict[str, str]:
    """The headers every request of the session carries."""
    headers = ctx.headers()
    if ctx.world_http_url is not None:
        headers[WORLD_URL_HEADER] = ctx.world_http_url
    if ctx.world_mcp_url is not None:
        headers[WORLD_MCP_URL_HEADER] = ctx.world_mcp_url
    return headers


def template_values(ctx: SessionContext, *, message: str, session_id: str) -> dict[str, str]:
    """The value of every request template placeholder (``templates.PLACEHOLDERS``)."""
    return {
        "message": message,
        "session_id": session_id,
        "trial_id": ctx.trial_id,
        "world_url": ctx.world_http_url or "",
        "world_mcp_url": ctx.world_mcp_url or "",
    }
