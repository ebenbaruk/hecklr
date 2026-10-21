"""Hecklr simulated customer: scripted, guided, replay and adversarial modes.

Wiring (done by the orchestrator, once per run)::

    factory = make_simulator(config.models.simulator)
    simulator = factory(brief, llm)          # per trial; a CustomerSimulator
    move = await simulator.next_turn(dialogue)
"""

from hecklr_sim.decision import ConsistencyVerdict, SimDecision
from hecklr_sim.errors import SimulatorError
from hecklr_sim.factory import make_simulator
from hecklr_sim.guard import ConsistencyGuard, GuardIssue
from hecklr_sim.guided import GuidedSimulator, Patience, SimulatorSettings
from hecklr_sim.scripted import ScriptedSimulator
from hecklr_sim.seeds import derive_seed, unit_draw
from hecklr_sim.strategies import (
    BUILTIN_STRATEGIES,
    CROSS_CUSTOMER_DATA_PROBE,
    REFUND_SOCIAL_ENGINEERING,
    AttackStrategy,
    resolve_strategy,
)

__version__ = "0.1.0"

__all__ = [
    "BUILTIN_STRATEGIES",
    "CROSS_CUSTOMER_DATA_PROBE",
    "REFUND_SOCIAL_ENGINEERING",
    "AttackStrategy",
    "ConsistencyGuard",
    "ConsistencyVerdict",
    "GuardIssue",
    "GuidedSimulator",
    "Patience",
    "ScriptedSimulator",
    "SimDecision",
    "SimulatorError",
    "SimulatorSettings",
    "derive_seed",
    "make_simulator",
    "resolve_strategy",
    "unit_draw",
]
