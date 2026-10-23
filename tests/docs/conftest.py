"""Put this directory on ``sys.path`` so the tests can import ``docs_snippets``."""

import sys
from pathlib import Path

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
