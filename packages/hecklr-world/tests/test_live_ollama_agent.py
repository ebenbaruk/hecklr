"""A real model (local Ollama) drives the sandbox over MCP. Opt in with ``pytest -m live``."""

import json
import os
from collections.abc import Callable
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.types import TextContent

from hecklr_schemas import TrialBrief, World
from hecklr_world import Sandbox, WorldChange, diff_snapshots

MODEL = os.environ.get("HECKLR_LIVE_OLLAMA_MODEL", "ollama/qwen3:4b-instruct").removeprefix(
    "ollama/"
)
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")

pytestmark = pytest.mark.live

BriefFactory = Callable[..., TrialBrief]
SinkFactory = Callable[..., Any]


def _ollama_has_model() -> bool:
    try:
        response = httpx2.get(f"{OLLAMA_URL}/models", timeout=2.0)
    except httpx2.HTTPError:
        return False
    return MODEL in {entry["id"] for entry in response.json().get("data", [])}


@pytest.fixture(scope="module", autouse=True)
def _require_ollama() -> None:
    if not _ollama_has_model():
        pytest.skip(f"Ollama at {OLLAMA_URL} with {MODEL} is not available")


@pytest.mark.anyio
async def test_live_model_reschedules_through_the_mcp_sandbox(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief()
    sink = make_sink(brief.trial_id)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a maintenance company's support agent. Use tools."},
        {"role": "user", "content": "I'm C-1042. Move appointment A-1 to 2026-10-28 at 09:00."},
    ]
    async with Sandbox() as sandbox, httpx2.AsyncClient(timeout=120.0) as http:
        world = await sandbox.open(brief, example_world, sink)
        before = world.snapshot()
        assert world.mcp_url is not None
        async with Client(world.mcp_url) as mcp:
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in (await mcp.list_tools()).tools
            ]
            for _ in range(4):
                body = {"model": MODEL, "messages": messages, "tools": tools, "temperature": 0}
                reply = await http.post(f"{OLLAMA_URL}/chat/completions", json=body)
                message = reply.json()["choices"][0]["message"]
                messages.append(message)
                if not message.get("tool_calls"):
                    break
                for call in message["tool_calls"]:
                    args = json.loads(call["function"]["arguments"] or "{}")
                    result = await mcp.call_tool(call["function"]["name"], args)
                    [block] = result.content
                    assert isinstance(block, TextContent)
                    messages.append(
                        {"role": "tool", "tool_call_id": call["id"], "content": block.text}
                    )
        changes = diff_snapshots(before, world.snapshot())
        await world.close()

    assert (
        WorldChange(
            path="appointments['A-1'].slot",
            kind="changed",
            before="2026-10-23T09:00",
            after="2026-10-28T09:00",
        )
        in changes
    )
    assert "reschedule_appointment" in [c.tool for c in sink.calls()]
