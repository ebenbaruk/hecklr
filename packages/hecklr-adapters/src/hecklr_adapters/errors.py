"""What can go wrong when talking to an agent, split by whose fault it is.

The channel driver turns ``AgentError`` into an ``agent_error`` (or ``timeout``) trial and keeps
the run going. Every other exception from an adapter is treated as a harness failure, which the
orchestrator retries and never counts against the agent (ADR 0006a).
"""


class AgentError(Exception):
    """The agent failed: unreachable, an HTTP error status, a malformed reply, or it raised."""


class AgentTimeoutError(AgentError):
    """The agent did not answer within the adapter's network timeout."""


class AdapterConfigError(ValueError):
    """The adapter configuration cannot work; raised before any trial starts."""
