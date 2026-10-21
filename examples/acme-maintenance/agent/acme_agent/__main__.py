"""Command line: start the agent's HTTP server.

    uv run python examples/acme-maintenance/agent/serve.py --brain rules --fault F3 --port 8000

Flags override the ``ACME_*`` environment variables (see ``AgentSettings.from_env``).
"""

import argparse
import dataclasses
import sys
from collections.abc import Sequence
from pathlib import Path

import anyio
import uvicorn

from acme_agent.agent import AcmeAgent
from acme_agent.config import (
    PROJECT_ROOT,
    WORLD_FILE,
    AgentConfigError,
    AgentSettings,
    load_agent_files,
)
from acme_agent.server import create_app
from hecklr_llm import ProviderRouter
from hecklr_world import WorldConfigError, load_world


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """The command line flags."""
    parser = argparse.ArgumentParser(prog="acme-agent", description=__doc__.splitlines()[0])
    parser.add_argument("--brain", choices=["llm", "rules"], help="default: llm (ACME_BRAIN)")
    parser.add_argument("--model", help="LLM brain model, provider/model (ACME_MODEL)")
    variant = parser.add_mutually_exclusive_group()
    variant.add_argument("--fault", help="fault variant to run, e.g. F3 (ACME_FAULT)")
    variant.add_argument("--overlay", type=Path, help="overlay directory (ACME_OVERLAY)")
    parser.add_argument(
        "--world-base-url", help="Hecklr world sandbox root URL (ACME_WORLD_BASE_URL)"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser.parse_args(argv)


def settings_from(args: argparse.Namespace) -> AgentSettings:
    """Environment settings with the given flags applied on top."""
    overrides = {
        "brain": args.brain,
        "model": args.model,
        "fault": args.fault,
        "overlay": args.overlay,
        "world_base_url": args.world_base_url,
    }
    settings = AgentSettings.from_env()
    if args.fault is not None or args.overlay is not None:
        settings = dataclasses.replace(settings, fault=None, overlay=None)
    return dataclasses.replace(settings, **{k: v for k, v in overrides.items() if v is not None})


async def serve(settings: AgentSettings, host: str, port: int) -> None:
    """Run the server until interrupted."""
    files = load_agent_files(overlay=settings.overlay_dir())
    world = load_world(PROJECT_ROOT / WORLD_FILE)
    async with ProviderRouter() as llm:
        agent = AcmeAgent(files, settings, llm if settings.brain == "llm" else None)
        config = uvicorn.Config(create_app(agent, world), host=host, port=port)
        await uvicorn.Server(config).serve()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = parse_args(argv)
    try:
        settings = settings_from(args)
        anyio.run(serve, settings, args.host, args.port)
    except (AgentConfigError, WorldConfigError) as exc:
        sys.stderr.write(f"acme-agent: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
