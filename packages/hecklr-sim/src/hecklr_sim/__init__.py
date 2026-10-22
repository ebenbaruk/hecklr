"""Hecklr simulated customer: scripted, guided, replay and adversarial modes.

Wiring (done by the orchestrator, once per run)::

    factory = make_simulator(config.models.simulator)
    simulator = factory(brief, llm)          # per trial; a CustomerSimulator
    move = await simulator.next_turn(dialogue)

``hecklr replay FILE`` calls ``transcripts_to_scenarios``.
"""

from hecklr_sim.adversarial import (
    ATTACK_PACK,
    INJECTION_CANARY,
    AttackTemplate,
    make_adversarial_simulator,
)
from hecklr_sim.decision import ConsistencyVerdict, SimDecision
from hecklr_sim.errors import ReplayExtractionError, SimulatorError
from hecklr_sim.factory import make_simulator
from hecklr_sim.guard import ConsistencyGuard, GuardIssue
from hecklr_sim.guided import GuidedSimulator, Patience, SimulatorSettings
from hecklr_sim.replay import (
    ExtractedFact,
    ReplayBatch,
    ReplayExtraction,
    ReplayFailure,
    extract_scenario,
    grounded_facts,
    load_conversations,
    scenario_id_for,
    scenario_to_yaml,
    transcripts_to_scenarios,
)
from hecklr_sim.scripted import ScriptedSimulator
from hecklr_sim.seeds import derive_subseed, unit_draw
from hecklr_sim.strategies import (
    BUILTIN_STRATEGIES,
    CROSS_CUSTOMER_DATA_PROBE,
    REFUND_SOCIAL_ENGINEERING,
    AttackStrategy,
    resolve_strategy,
)

__version__ = "0.1.0"

__all__ = [
    "ATTACK_PACK",
    "BUILTIN_STRATEGIES",
    "CROSS_CUSTOMER_DATA_PROBE",
    "INJECTION_CANARY",
    "REFUND_SOCIAL_ENGINEERING",
    "AttackStrategy",
    "AttackTemplate",
    "ConsistencyGuard",
    "ConsistencyVerdict",
    "ExtractedFact",
    "GuardIssue",
    "GuidedSimulator",
    "Patience",
    "ReplayBatch",
    "ReplayExtraction",
    "ReplayExtractionError",
    "ReplayFailure",
    "ScriptedSimulator",
    "SimDecision",
    "SimulatorError",
    "SimulatorSettings",
    "derive_subseed",
    "extract_scenario",
    "grounded_facts",
    "load_conversations",
    "make_adversarial_simulator",
    "make_simulator",
    "resolve_strategy",
    "scenario_id_for",
    "scenario_to_yaml",
    "transcripts_to_scenarios",
    "unit_draw",
]
