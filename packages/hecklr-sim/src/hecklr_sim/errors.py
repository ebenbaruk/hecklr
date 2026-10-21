"""Errors raised by the simulator."""


class SimulatorError(Exception):
    """The simulator could not produce a valid turn; a harness failure, never an agent failure.

    Raised when every regeneration attempt still contradicted the brief or returned malformed
    output. The orchestrator retries the trial and, after its attempts, marks it
    ``harness_error`` so a broken simulation is excluded from statistics instead of being counted
    against the agent.
    """


class ReplayExtractionError(Exception):
    """The extractor's reply for one conversation cannot become a scenario (e.g. no goal).

    A data problem with one conversation, so ``transcripts_to_scenarios`` reports it as a
    ``ReplayFailure`` and keeps the batch; programming errors still propagate.
    """
