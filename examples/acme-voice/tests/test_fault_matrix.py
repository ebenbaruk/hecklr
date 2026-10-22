"""SC8 for voice, at the driver level: the clean agent passes, F7-F9 each fail on their metric.

Each case is a real graded trial over a localhost WebSocket (see ``voice_trials``): Hecklr's
``VoiceDriver`` measures the agent's turn-taking and ``hecklr_grade`` applies the scenario's
gate. The callers speak synthetic speech and the agent hears the script (``ScriptedASR``), so
this matrix is free and needs no model; F10 is a speech-recognition fault and is proven with
real speech (``scripts/voice-fault-matrix.sh``, ``RESULTS.md``).
"""

import anyio
import pytest
from voice_trials import VoiceOutcome, run_voice_trial, scenario, synthetic_persona, variant

from hecklr_world import Sandbox

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.voice,
    pytest.mark.enable_socket,
    pytest.mark.allow_hosts(["127.0.0.1"]),
    pytest.mark.usefixtures("frozen_heap"),
]

CASES = {
    "latency": ("timing/latency-cancel-a3003.yaml", "steady-caller"),
    "barge_in": ("timing/barge-in-cancel.yaml", "interrupter"),
    "hesitation": ("timing/hesitant-cancel-a3005.yaml", "hesitant-caller"),
}
CATCHES = {
    "F7": ("latency", "voice:response_latency"),
    "F8": ("barge_in", "voice:barge_in_reaction"),
    "F9": ("hesitation", "voice:false_interruptions"),
}


async def run_matrix(variants: list[str]) -> dict[tuple[str, str], VoiceOutcome]:
    outcomes: dict[tuple[str, str], VoiceOutcome] = {}

    async def one(sandbox: Sandbox, name: str, case: str) -> None:
        path, persona = CASES[case]
        outcomes[name, case] = await run_voice_trial(
            sandbox, variant(name), scenario(path), synthetic_persona(persona)
        )

    async with Sandbox() as sandbox, anyio.create_task_group() as group:
        for name in variants:
            for case in CASES:
                group.start_soon(one, sandbox, name, case)
    return outcomes


async def test_sc8_clean_voice_agent_passes_and_each_timing_fault_fails_its_metric() -> None:
    outcomes = await run_matrix(["clean", "F7", "F8", "F9"])
    for case in CASES:
        clean = outcomes["clean", case]
        assert clean.trial.status == "ok", (case, clean.trial)
        assert clean.passed, (case, clean.failed(), clean.transcript())
    for fault, (case, grader) in CATCHES.items():
        assert grader in outcomes[fault, case].failed(), (fault, outcomes[fault, case].grades)
    # Each fault leaves the other metrics alone (F9's cut-up turns may also break the task).
    gate_of_case = dict(CATCHES.values())
    for fault, (own_case, _) in CATCHES.items():
        for case, grader in gate_of_case.items():
            if case != own_case:
                assert grader not in outcomes[fault, case].failed(), (fault, case)
