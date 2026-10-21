"""Errors raised by the simulator."""


class SimulatorError(Exception):
    """The simulator could not produce a valid turn; a harness failure, never an agent failure.

    Raised when every regeneration attempt still contradicted the brief or returned malformed
    output. The orchestrator retries the trial and, after its attempts, marks it
    ``harness_error`` so a broken simulation is excluded from statistics instead of being counted
    against the agent.
    """
