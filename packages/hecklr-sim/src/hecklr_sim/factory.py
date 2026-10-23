"""``make_simulator``: the ``SimulatorFactory`` the orchestrator calls once per trial."""

from collections.abc import Callable, Mapping

from hecklr_schemas import (
    Clock,
    CustomerSimulator,
    LLMProvider,
    MonotonicClock,
    SimulationMode,
    SimulatorFactory,
    TrialBrief,
)
from hecklr_sim.guided import DEFAULT_MAX_TOKENS, GuidedSimulator, SimulatorSettings
from hecklr_sim.scripted import ScriptedSimulator
from hecklr_sim.strategies import BUILTIN_STRATEGIES, AttackStrategy, resolve_strategy


def make_simulator(
    model: str,
    *,
    max_regenerations: int = 2,
    llm_consistency_check: bool = False,
    temperature: float | None = None,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    strategies: Mapping[str, AttackStrategy] = BUILTIN_STRATEGIES,
    clock_factory: Callable[[], Clock] = MonotonicClock,
) -> SimulatorFactory:
    """Return a factory that builds the right simulator for each trial's brief.

    - ``scripted`` scenarios get a ``ScriptedSimulator`` (no model calls).
    - Every other mode gets a ``GuidedSimulator`` calling ``model`` through the trial's ``llm``.
      A persona with an ``adversarial`` strategy follows it in any non-scripted mode, so red-team
      personas can be crossed with ordinary scenarios; ``adversarial`` mode requires one.

    Args:
        model: The simulator model, ``provider/model``.
        max_regenerations: Extra drafts per turn after the guard rejects one.
        llm_consistency_check: Also have the model audit each draft (catches a changed goal).
        temperature: Sampling temperature; ``None`` keeps the provider default.
        max_tokens: Reply budget per decision.
        strategies: Attack strategies by id; extend ``BUILTIN_STRATEGIES`` to add a pack.
        clock_factory: Makes each trial's clock (for ``max_duration_s``); started at creation.

    Raises:
        ValueError: At factory call time, for an adversarial scenario without a strategy or a
            persona naming an unknown strategy. These are configuration errors of the trial.
    """
    settings = SimulatorSettings(
        model=model,
        max_regenerations=max_regenerations,
        llm_consistency_check=llm_consistency_check,
        temperature=temperature,
        max_tokens=max_tokens,
    )

    def build(brief: TrialBrief, llm: LLMProvider) -> CustomerSimulator:
        scenario, persona = brief.scenario, brief.persona
        if scenario.mode is SimulationMode.SCRIPTED:
            return ScriptedSimulator(brief, clock_factory())
        if scenario.mode is SimulationMode.ADVERSARIAL and persona.adversarial is None:
            raise ValueError(
                f"scenario {scenario.id!r} is adversarial but persona {persona.id!r} names no "
                "attack strategy"
            )
        strategy = resolve_strategy(persona.adversarial, strategies)
        return GuidedSimulator(brief, llm, settings, clock_factory(), strategy)

    return build
