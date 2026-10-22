"""Start the loopback agent without installing it.

Python puts this script's directory on ``sys.path``, which makes ``loopback_agent`` importable::

    uv run python examples/voice-loopback/serve.py --port 8765 --reply-after-ms 600
"""

from loopback_agent.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
