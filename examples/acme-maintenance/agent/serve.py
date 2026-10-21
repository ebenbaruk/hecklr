"""Start the acme-maintenance agent without installing it.

Python puts this script's directory on ``sys.path``, which makes ``acme_agent`` importable::

    uv run python examples/acme-maintenance/agent/serve.py --brain rules --fault F3 --port 8000
"""

from acme_agent.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
