"""Make the reference agents importable from a benchmark agent script, without installing them.

``acme_agent`` (the brain, its tools and world) lives in ``examples/acme-maintenance/agent`` and
``acme_voice`` (the cascaded call pipeline) in ``examples/acme-voice/agent``; the benchmark
reuses both instead of copying them, so every voice stack runs exactly the same brain.
"""

import sys
from pathlib import Path
from typing import Final

REPO: Final = Path(__file__).resolve().parents[3]
AGENT_PATHS: Final = (
    REPO / "examples" / "acme-maintenance" / "agent",
    REPO / "examples" / "acme-voice" / "agent",
)

for path in AGENT_PATHS:
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
