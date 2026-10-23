"""Process exit codes of the ``hecklr`` commands.

0-3 match ``hecklr gate`` (pass, regression, inconclusive, harness error), so a script can read
them the same way across commands. The rest come from ``sysexits.h`` or are Hecklr's own, chosen
to never collide with the gate's.
"""

from typing import Final

OK: Final = 0
CHECK_FAILED: Final = 1
"""``hecklr doctor`` found a problem; ``hecklr replay`` lost some conversations."""
HARNESS_ERROR: Final = 3
"""The run as a whole failed: it crashed, or no trial could be run."""
BUDGET: Final = 4
"""The run was refused (estimate over budget) or stopped (spend reached the budget)."""
INTERRUPTED: Final = 130
"""128 + SIGINT, the shell's code for a command stopped with Ctrl-C."""
NOT_IMPLEMENTED: Final = 69
"""``EX_UNAVAILABLE``: a command or feature that is not built yet."""
CONFIG_ERROR: Final = 78
"""``EX_CONFIG``: an authored file, flag or adapter configuration is invalid."""
