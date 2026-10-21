"""acme-maintenance: Hecklr's reference support agent (chat) and its fault variants.

The LLM brain (``LLMBrain``) is the real reference agent. The rules brain (``RulesBrain``) is a
deterministic stand-in that reads the same prompt and tool-description files, so the fault matrix
can run in CI for free; it is not a model of good agent design.
"""

from acme_agent.agent import AcmeAgent
from acme_agent.config import (
    PROJECT_ROOT,
    WORLD_FILE,
    AgentConfigError,
    AgentFiles,
    AgentSettings,
    fault_overlay,
    load_agent_files,
)
from acme_agent.inprocess import InProcessAgent
from acme_agent.server import create_app
from acme_agent.tools import HttpWorldTools, LocalWorldTools, Reply, ToolBackend, ToolOutcome

__all__ = [
    "PROJECT_ROOT",
    "WORLD_FILE",
    "AcmeAgent",
    "AgentConfigError",
    "AgentFiles",
    "AgentSettings",
    "HttpWorldTools",
    "InProcessAgent",
    "LocalWorldTools",
    "Reply",
    "ToolBackend",
    "ToolOutcome",
    "create_app",
    "fault_overlay",
    "load_agent_files",
]
