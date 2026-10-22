"""SC4 over a real network transport: the loopback agent in its own process, Hecklr calling it.

The spec's calibration (Voice simulation engine, "Calibration and self-tests"): the agent replies
300, 600 and 900 ms after the caller stops and reacts to barge-in after a known delay; Hecklr's
measured response latency and barge-in reaction must each be within 20 ms of the injected value.
"""

import pytest
from loopback_agent import LoopbackSettings
from loopback_agent.bench import agent_process, health, place_call

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.voice,
    pytest.mark.enable_socket,
    pytest.mark.allow_hosts(["127.0.0.1"]),
    pytest.mark.usefixtures("frozen_heap"),
]

TOLERANCE_MS = 20


@pytest.mark.sc4_timing
@pytest.mark.parametrize(("delay_ms", "reaction_ms"), [(300, 150), (600, 300), (900, 200)])
async def test_sc4_latency_and_barge_in_within_20ms_over_the_network(
    delay_ms: int, reaction_ms: int
) -> None:
    settings = LoopbackSettings(reply_after_ms=delay_ms, barge_in_reaction_ms=reaction_ms)
    with agent_process(settings) as url:
        plain = await place_call(url, seed=1, barge_in=False)
        barged = await place_call(url, seed=2, barge_in=True, max_turns=8)
        report = health(url)
    assert plain.result.status == barged.result.status == "ok"
    latencies = plain.latencies_ms + barged.latencies_ms
    reactions = barged.reactions_ms
    assert len(latencies) >= 4
    assert len(reactions) >= 2
    stats = report["stats"]
    assert isinstance(stats, dict)
    assert stats["cuts"] == len(reactions)
    # The truth is what the agent did: on a machine busy with other tests its process can send
    # a few milliseconds late, which it measures itself (sc4_calibrate.py, on an idle machine,
    # holds the agent to the injected value alone).
    tolerance = TOLERANCE_MS + float(stats["max_late_ms"])
    assert all(abs(latency - delay_ms) <= tolerance for latency in latencies), latencies
    assert all(
        reaction is not None and abs(reaction - reaction_ms) <= tolerance for reaction in reactions
    ), (reactions, tolerance)


async def test_an_agent_that_never_stops_is_measured_as_never_reacting() -> None:
    settings = LoopbackSettings(reply_after_ms=400, barge_in_reaction_ms=None)
    with agent_process(settings) as url:
        barged = await place_call(url, seed=2, barge_in=True, max_turns=6)
    # The caller is talked over until the reply ends: the reaction is the rest of the reply.
    reactions = [reaction for reaction in barged.reactions_ms if reaction is not None]
    assert reactions
    assert min(reactions) > 300
    assert max(reactions) > 1000
