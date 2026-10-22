"""Start the acme email agent without installing it.

Python puts this script's directory on ``sys.path``, which makes ``acme_email`` importable; the
brains it reuses (``acme_agent``) live next door in ``examples/acme-maintenance/agent``::

    uv run python examples/acme-email/agent/serve.py webhook --brain rules --fault F11
"""

import sys
from pathlib import Path

# Importable only once the brains' directory is on the path.
sys.path.insert(1, str(Path(__file__).resolve().parents[2] / "acme-maintenance" / "agent"))

from acme_email.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
