"""A minimal voice support agent behind the ``websocket`` voice adapter's raw PCM protocol.

On the ``start`` message it looks the caller's visit up in the trial's world (``world_url``,
ADR 0007e), so a trial's ``tool_called`` criterion proves the world URL reached a voice agent.
It then answers every caller utterance ``REPLY_AFTER_MS`` after the caller's last speech sample,
with tagged synthetic speech that ``FakeASR`` transcribes exactly, so ``hecklr run`` can measure
its response latency against a known truth. The timing comes from ``hecklr_voice.testing``'s
loopback; this module only puts it behind a socket.

The agent runs on its own event loop in a thread, as a real agent runs in its own process: the
harness's loop stalls must not move the agent's clock. Run it alone with
``python packages/hecklr/tests/fixtures/voice_agent.py --port 8765``.
"""

import argparse
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Final

import anyio
import httpx2
from websockets.asyncio.server import ServerConnection, serve

from hecklr_schemas import SessionContext, TrialId
from hecklr_voice.testing import LoopbackVoiceTarget, synthetic_reply

REPLY_AFTER_MS: Final = 500
CUSTOMER_ID: Final = "C-1001"


@dataclass
class VoiceAgentLog:
    """What the agent saw, for assertions."""

    starts: list[dict[str, object]] = field(default_factory=list)
    """Every ``start`` message."""
    lookups: list[int] = field(default_factory=list)
    """HTTP status of each world lookup."""


def _answer(world_url: object) -> tuple[str, int | None]:
    """The reply for this call, from the trial's world when the start message names one."""
    if not isinstance(world_url, str):
        return "sorry our booking system is offline today", None
    response = httpx2.post(
        f"{world_url}/tools/get_appointment", json={"customer_id": CUSTOMER_ID}, timeout=10
    )
    if response.status_code != 200:
        return "sorry I could not find your visit", response.status_code
    slot = str(response.json()["slot"]).replace("T", " at ")
    return f"your boiler visit is booked for {slot}", response.status_code


async def _call(connection: ServerConnection, log: VoiceAgentLog) -> None:
    start = json.loads(await connection.recv())
    log.starts.append(start)
    text, status = await anyio.to_thread.run_sync(_answer, start.get("world_url"))
    if status is not None:
        log.lookups.append(status)
    agent = LoopbackVoiceTarget(reply=synthetic_reply(text), reply_after_ms=REPLY_AFTER_MS)
    link = await agent.connect(SessionContext(trial_id=TrialId(str(start["trial_id"])), seed=0))

    async def speak() -> None:
        async for frame in link.frames():
            await connection.send(frame.pcm)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(speak)
        async for message in connection:
            if isinstance(message, bytes):
                await link.send_frame(message, 16_000)
            elif json.loads(message).get("type") == "stop":
                break
        tasks.cancel_scope.cancel()
    await link.hangup()


@contextmanager
def serve_in_thread(port: int = 0) -> Iterator[tuple[str, VoiceAgentLog]]:
    """Serve the agent on 127.0.0.1 (``port`` 0: ephemeral); yields its URL and log."""
    log = VoiceAgentLog()
    ready = threading.Event()
    address: list[str] = []
    stop = threading.Event()

    async def main() -> None:
        async with serve(lambda c: _call(c, log), "127.0.0.1", port) as server:
            bound = next(iter(server.sockets)).getsockname()[1]
            address.append(f"ws://127.0.0.1:{bound}/")
            ready.set()
            while not stop.is_set():  # noqa: ASYNC110 -- stopped from another thread
                await anyio.sleep(0.05)

    thread = threading.Thread(target=anyio.run, args=(main,), daemon=True)
    thread.start()
    if not ready.wait(10):
        raise RuntimeError("the voice agent did not start")
    try:
        yield address[0], log
    finally:
        stop.set()
        thread.join(timeout=10)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    port = parser.parse_args().port
    with serve_in_thread(port) as (url, _):
        print(f"voice agent listening on {url}", flush=True)
        threading.Event().wait()
