"""Hecklr fixer: candidate prompt patches validated against the suite (spec, "Fixer", US-10).

Typical use (what ``hecklr fix`` does)::

    editable = load_editable(project.config, project.config_path)
    target = mined_target(load_clusters(mine_root), load_queue(mine_root), cluster_id)
    fixer = Fixer(config_path=..., editable=editable, target=target, settings=FixSettings(...),
                  llm=llm, runner=suite_runner, launcher=AgentCommand("{python} serve.py "
                  "--port {port}"), session_dir=output_dir / FIX_DIR / fix_id,
                  runs_output_dir=output_dir / FIX_DIR)
    result = await fixer.run()
    write_session(session_dir, result, guard_suite="ci", report=render_markdown(result, ...))
"""

from hecklr_fix.agent import AgentCommand, free_port
from hecklr_fix.editable import EditableFiles, load_editable, protected_files
from hecklr_fix.errors import FixError, PatchRejectedError
from hecklr_fix.fixer import (
    AgentLauncher,
    Fixer,
    FixOutcome,
    FixResult,
    FixSettings,
    PassRequest,
    SuiteRunner,
)
from hecklr_fix.generate import Candidate, RejectedProposal, failure_evidence, generate_candidates
from hecklr_fix.patching import ProposedEdit, ProposedPatch, apply_edits, unified_diff
from hecklr_fix.pr import PullRequest, open_pull_request
from hecklr_fix.report import render_markdown
from hecklr_fix.store import FIX_DIR, FixSummary, write_session
from hecklr_fix.target import FixTarget, mined_target, target_from_run
from hecklr_fix.validation import SuiteRun, cell_outcomes, compare_cells, improved, regressions
from hecklr_fix.workspace import TARGETED_SUITE, prepare_workspace

__version__ = "0.1.0"

__all__ = [
    "FIX_DIR",
    "TARGETED_SUITE",
    "AgentCommand",
    "AgentLauncher",
    "Candidate",
    "EditableFiles",
    "FixError",
    "FixOutcome",
    "FixResult",
    "FixSettings",
    "FixSummary",
    "FixTarget",
    "Fixer",
    "PassRequest",
    "PatchRejectedError",
    "ProposedEdit",
    "ProposedPatch",
    "PullRequest",
    "RejectedProposal",
    "SuiteRun",
    "SuiteRunner",
    "__version__",
    "apply_edits",
    "cell_outcomes",
    "compare_cells",
    "failure_evidence",
    "free_port",
    "generate_candidates",
    "improved",
    "load_editable",
    "mined_target",
    "open_pull_request",
    "prepare_workspace",
    "protected_files",
    "regressions",
    "render_markdown",
    "target_from_run",
    "unified_diff",
    "write_session",
]
