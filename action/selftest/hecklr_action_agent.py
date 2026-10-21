"""The acme-maintenance reference agent, in process, for the Action's own end-to-end test.

The self-test workflow copies this module and ``hecklr.yaml`` into ``examples/acme-maintenance``
and runs the scripted ``smoke`` suites: the rules brain and scripted customers call no model, so
the test is free and exactly repeatable. ``ACME_FAULT`` selects a seeded fault (``F1``-``F6``).

The ``python`` adapter hands each trial its own world sandbox URL, which the in-process agent's
tools call; that is why the test does not start the HTTP server.
"""

import sys
from dataclasses import replace
from pathlib import Path

_AGENT_DIR = Path(__file__).resolve().parent / "agent"
if str(_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_AGENT_DIR))

# Importable only once the agent directory is on sys.path.
from acme_agent import (  # noqa: E402
    PROJECT_ROOT,
    WORLD_FILE,
    AcmeAgent,
    AgentSettings,
    InProcessAgent,
    load_agent_files,
)

from hecklr_world import load_world  # noqa: E402

_SETTINGS = replace(AgentSettings.from_env(), brain="rules")

reply = InProcessAgent(
    AcmeAgent(load_agent_files(overlay=_SETTINGS.overlay_dir()), _SETTINGS),
    load_world(PROJECT_ROOT / WORLD_FILE),
)
"""The ``python`` adapter entry point: ``hecklr_action_agent:reply``."""
