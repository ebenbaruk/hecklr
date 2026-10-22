"""``python -m loopback_agent`` (or ``serve.py``): run the networked loopback agent.

It prints ``listening on ws://<host>:<port>/`` once it accepts calls (with ``--port 0`` the port
is picked by the OS), so a script can start it and read where to call.
"""

import argparse
import gc
import signal
import sys
from collections.abc import Sequence

import anyio

from loopback_agent.call import DEFAULT_REPLY_TEXT, LoopbackSettings
from loopback_agent.server import LoopbackServer, bound_port, running_server


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(prog="loopback_agent", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--reply-after-ms", type=float, default=600.0)
    parser.add_argument(
        "--barge-in-reaction-ms",
        type=_reaction,
        default=200.0,
        help="milliseconds, or 'none' to never stop for a barge-in",
    )
    parser.add_argument("--endpoint-ms", type=float, default=200.0)
    parser.add_argument("--greeting", action="store_true", help="say the reply once on connect")
    parser.add_argument("--reply-text", default=DEFAULT_REPLY_TEXT, help="what every reply says")
    return parser.parse_args(argv)


def _reaction(value: str) -> float | None:
    return None if value.lower() == "none" else float(value)


def settings_from(args: argparse.Namespace) -> LoopbackSettings:
    """The injected timings the options ask for."""
    return LoopbackSettings(
        reply_after_ms=args.reply_after_ms,
        barge_in_reaction_ms=args.barge_in_reaction_ms,
        endpoint_ms=args.endpoint_ms,
        greeting=args.greeting,
        reply_text=args.reply_text,
    )


async def serve_until_signalled(loopback: LoopbackServer, host: str, port: int) -> None:
    """Serve until SIGINT or SIGTERM."""
    async with running_server(loopback, host, port) as server:
        # A long-lived server keeps its start-up objects out of the collector's way: a full
        # collection would pause the loop, and with it every call's audio (ADR 0009a).
        gc.collect()
        gc.freeze()
        print(f"listening on ws://{host}:{bound_port(server)}/", flush=True)  # noqa: T201 -- the CLI's output
        with anyio.open_signal_receiver(signal.SIGINT, signal.SIGTERM) as signals:
            async for _ in signals:
                return


def main(argv: Sequence[str] | None = None) -> int:
    """Run the agent; exit status 2 on invalid settings."""
    args = parse_args(argv)
    try:
        loopback = LoopbackServer(settings_from(args))
    except ValueError as exc:
        print(f"loopback_agent: {exc}", file=sys.stderr)  # noqa: T201 -- the CLI's output
        return 2
    anyio.run(serve_until_signalled, loopback, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
