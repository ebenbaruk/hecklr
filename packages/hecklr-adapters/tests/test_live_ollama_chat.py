"""The openai-compatible adapter against a real local Ollama. Opt in with ``pytest -m live``."""

import os

import httpx2
import pytest
from pydantic import TypeAdapter

from hecklr_adapters import make_chat_target
from hecklr_schemas import ChatAdapterConfig, SessionContext, TrialId, new_id

MODEL = os.environ.get("HECKLR_LIVE_OLLAMA_MODEL", "ollama/qwen3:4b-instruct").removeprefix(
    "ollama/"
)
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434/v1")

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture(scope="module", autouse=True)
def _require_ollama() -> None:
    try:
        names = {m["id"] for m in httpx2.get(f"{OLLAMA_URL}/models", timeout=2.0).json()["data"]}
    except httpx2.HTTPError:
        names = set()
    if MODEL not in names:
        pytest.skip(f"Ollama at {OLLAMA_URL} with {MODEL} is not available")


@pytest.mark.parametrize("stream", [True, False])
async def test_live_openai_compatible_adapter_keeps_memory_against_ollama(stream: bool) -> None:
    config = TypeAdapter[ChatAdapterConfig](ChatAdapterConfig).validate_python(
        {"type": "openai-compatible", "url": OLLAMA_URL, "model": MODEL, "stream": stream}
    )
    session = await make_chat_target(config).open(
        SessionContext(trial_id=TrialId(new_id()), seed=1)
    )
    _ = [c async for c in session.send("My customer id is C-1042. Reply with just OK.")]
    chunks = [c async for c in session.send("What is my customer id? Reply with only the id.")]
    summary = await session.close()

    assert "C-1042" in "".join(chunk.text for chunk in chunks)
    assert chunks[-1].final
    assert summary.turns == 2
    if stream:
        assert sum(bool(chunk.text) for chunk in chunks) > 1
