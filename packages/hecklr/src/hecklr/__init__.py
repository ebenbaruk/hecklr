"""Hecklr: the crash-test lab for conversational AI agents.

The Python API mirrors the CLI::

    import hecklr

    @hecklr.agent(channel="chat")
    async def my_agent(session: hecklr.Session, message: str) -> str:
        return await support_bot.reply(session.id, message)

    results = hecklr.run("hecklr.yaml", suite="ci")      # or: await hecklr.run_async(...)

``pytest -p hecklr --hecklr-suite=ci`` runs a suite as one test per scenario.
"""

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hecklr.api import run, run_async
    from hecklr.sdk import Session, agent

__version__ = "0.1.0"

__all__ = ["Session", "__version__", "agent", "run", "run_async"]

# Loaded on first use: pytest imports this package for the plugin in every test session, which
# must not pay for the whole harness (DuckDB, the world server, model SDKs) when it is not used.
_LAZY = {
    "run": "hecklr.api",
    "run_async": "hecklr.api",
    "Session": "hecklr.sdk",
    "agent": "hecklr.sdk",
}


def __getattr__(name: str) -> object:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module 'hecklr' has no attribute {name!r}")
    return getattr(importlib.import_module(module), name)
