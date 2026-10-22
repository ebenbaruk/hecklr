"""Command line: run the email agent on a webhook or on a mailbox.

    uv run python examples/acme-email/agent/serve.py webhook --brain rules --port 8100
    uv run python examples/acme-email/agent/serve.py mailbox --brain rules --inbox support@acme.test

Flags override the chat agent's ``ACME_*`` environment variables (``AgentSettings.from_env``);
``--fault F11`` and ``--fault F12`` overlay ``mailer.yaml``, ``--fault F1``..``F6`` the brain files.
Mailbox credentials, when the server needs any, come from ``ACME_MAIL_USERNAME`` and
``ACME_MAIL_PASSWORD``.
"""

import argparse
import dataclasses
import os
import sys
from collections.abc import Sequence
from pathlib import Path

import anyio
import uvicorn
from acme_agent.agent import AcmeAgent
from acme_agent.config import PROJECT_ROOT, WORLD_FILE, AgentConfigError, AgentSettings

from acme_email.config import load_email_agent_files
from acme_email.desk import EmailDesk
from acme_email.mailbox import Mailbox, serve_mailbox
from acme_email.webhook import create_app
from hecklr_llm import ProviderRouter
from hecklr_schemas import World
from hecklr_world import WorldConfigError, load_world


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """The command line flags."""
    parser = argparse.ArgumentParser(prog="acme-email", description=__doc__.splitlines()[0])
    parser.add_argument("--brain", choices=["llm", "rules"], help="default: llm (ACME_BRAIN)")
    parser.add_argument("--model", help="LLM brain model, provider/model (ACME_MODEL)")
    variant = parser.add_mutually_exclusive_group()
    variant.add_argument("--fault", help="fault variant, e.g. F11 (ACME_FAULT)")
    variant.add_argument("--overlay", help="overlay directory (ACME_OVERLAY)")
    parser.add_argument(
        "--world-base-url", help="Hecklr world sandbox root URL (ACME_WORLD_BASE_URL)"
    )
    transports = parser.add_subparsers(dest="transport", required=True)
    webhook = transports.add_parser("webhook", help="answer Hecklr's webhook email adapter")
    webhook.add_argument("--host", default="127.0.0.1")
    webhook.add_argument("--port", type=int, default=8100)
    mailbox = transports.add_parser("mailbox", help="answer an IMAP inbox over SMTP")
    mailbox.add_argument("--inbox", default="support@acme.test")
    mailbox.add_argument("--smtp-host", default="localhost")
    mailbox.add_argument("--smtp-port", type=int, default=1025)
    mailbox.add_argument("--imap-host", default="localhost")
    mailbox.add_argument("--imap-port", type=int, default=1143)
    mailbox.add_argument("--poll-interval-s", type=float, default=1.0)
    return parser.parse_args(argv)


def settings_from(args: argparse.Namespace) -> AgentSettings:
    """Environment settings with the given flags applied on top."""
    settings = AgentSettings.from_env()
    if args.fault is not None or args.overlay is not None:
        settings = dataclasses.replace(settings, fault=None, overlay=None)
    overrides = {
        "brain": args.brain,
        "model": args.model,
        "fault": args.fault,
        "overlay": None if args.overlay is None else Path(args.overlay).resolve(),
        "world_base_url": args.world_base_url,
    }
    return dataclasses.replace(settings, **{k: v for k, v in overrides.items() if v is not None})


def mailbox_from(args: argparse.Namespace) -> Mailbox:
    """The mailbox flags, with credentials from the environment."""
    return Mailbox(
        inbox=args.inbox,
        smtp_host=args.smtp_host,
        smtp_port=args.smtp_port,
        imap_host=args.imap_host,
        imap_port=args.imap_port,
        username=os.environ.get("ACME_MAIL_USERNAME") or None,
        password=os.environ.get("ACME_MAIL_PASSWORD") or None,
        poll_interval_s=args.poll_interval_s,
    )


async def run(args: argparse.Namespace, settings: AgentSettings, world: World) -> None:
    """Serve until interrupted."""
    files = load_email_agent_files(overlay=settings.overlay_dir())
    async with ProviderRouter() as llm:
        agent = AcmeAgent(files.brain, settings, llm if settings.brain == "llm" else None)
        desk = EmailDesk(agent, files.mailer, files.variant)
        if args.transport == "mailbox":
            await serve_mailbox(
                desk, world, mailbox_from(args), world_base_url=settings.world_base_url
            )
        else:
            app = create_app(desk, world, world_base_url=settings.world_base_url)
            await uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port)).serve()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point; returns the process exit code."""
    args = parse_args(argv)
    try:
        settings = settings_from(args)
        world = load_world(PROJECT_ROOT / WORLD_FILE)
        anyio.run(run, args, settings, world)
    except (AgentConfigError, WorldConfigError) as exc:
        sys.stderr.write(f"acme-email: {exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
