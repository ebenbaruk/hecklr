"""``hecklr gate`` exit codes (spec, "CLI").

0 pass, 1 regression, 2 inconclusive (configurable), 3 harness error.
"""

from typing import Final

from hecklr_report.model import Analysis
from hecklr_schemas import GateConfig, Verdict

EXIT_PASS: Final = 0
EXIT_REGRESSION: Final = 1
EXIT_INCONCLUSIVE: Final = 2
EXIT_HARNESS_ERROR: Final = 3


def gate_exit_code(analysis: Analysis, gate: GateConfig) -> int:
    """The exit code of ``hecklr gate`` for ``analysis``.

    The suite pass-rate verdict decides (ADR 0005b): slice and latency verdicts are diagnostics,
    since a latency gate belongs in scenarios as a ``latency_p95_below`` or ``voice_metric``
    criterion. A run that cannot be trusted (``Analysis.problems``) or has no baseline comparison
    exits 3, so a broken harness never passes or fails a PR on missing data. An inconclusive
    verdict exits 2, or 0 when ``gate.on_inconclusive`` is ``pass``.
    """
    comparison = analysis.suite_comparison
    if analysis.problems or comparison is None:
        return EXIT_HARNESS_ERROR
    match comparison.verdict:
        case Verdict.REGRESSION:
            return EXIT_REGRESSION
        case Verdict.INCONCLUSIVE:
            return EXIT_INCONCLUSIVE if gate.on_inconclusive == "fail" else EXIT_PASS
        case Verdict.IMPROVEMENT | Verdict.NO_CHANGE:
            return EXIT_PASS
