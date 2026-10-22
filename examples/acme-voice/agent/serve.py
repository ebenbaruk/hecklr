"""Start the acme voice agent without installing it.

Python puts this script's directory on ``sys.path``, which makes ``acme_voice`` importable; the
acme brain (``acme_agent``) is imported from the chat reference agent, never copied::

    uv run python examples/acme-voice/agent/serve.py --brain rules --port 8765
"""

import sys
from pathlib import Path

CHAT_AGENT = Path(__file__).resolve().parents[2] / "acme-maintenance" / "agent"
if str(CHAT_AGENT) not in sys.path:
    sys.path.insert(0, str(CHAT_AGENT))

from acme_voice.__main__ import main  # noqa: E402 -- importable only once acme_agent is on the path

if __name__ == "__main__":
    raise SystemExit(main())
