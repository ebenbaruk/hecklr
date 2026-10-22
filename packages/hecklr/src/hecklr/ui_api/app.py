"""The FastAPI application behind ``hecklr ui``: a JSON API under ``/api`` and the built app.

Read-only on results; the only writes are human labels and proposal decisions (spec, "Local
UI"; ADR 0016a). Endpoints are synchronous: FastAPI runs them in its thread pool, so loading a
run or bootstrapping an analysis never blocks the event loop.
"""

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Final

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from hecklr import __version__
from hecklr.ui_api.labels import (
    LABELS_FILE,
    LabelBook,
    LabelFileError,
    UnknownGradeError,
    agreement,
    find_judge_grade,
    judge_grades,
    label_queue,
    utc_now,
)
from hecklr.ui_api.models import (
    AgreementReport,
    ClusterDetail,
    ClusterItem,
    ClusterMember,
    Decision,
    DecisionResult,
    LabelQueue,
    LabelRequest,
    Meta,
    ProposalItem,
    RunItem,
    RunSummary,
    TrialDetail,
)
from hecklr.ui_api.review import (
    InvalidScenarioError,
    MineFileError,
    MineFiles,
    ProposalDecidedError,
    UnknownProposalError,
)
from hecklr.ui_api.runs import RunBusyError, RunLibrary, UnknownRunError
from hecklr.ui_api.trials import UnknownTrialError, locate_recording, trial_detail
from hecklr_core import Project, RunResults
from hecklr_report import COST_PER_TRIAL, Analysis, RunInfo
from hecklr_schemas import HumanLabel, ProposalStatus
from hecklr_sim import scenario_to_yaml
from hecklr_stats import PASS_RATE

STATIC_DIR: Final = Path(__file__).parent / "static"
"""Where ``npm run build`` in ``ui/`` puts the app; shipped inside the wheel."""

DEFAULT_QUEUE_SIZE: Final = 50
"""US-11: "I label 50 judged trials"."""

_ERRORS: Final[dict[type[Exception], int]] = {
    UnknownRunError: 404,
    UnknownTrialError: 404,
    UnknownGradeError: 404,
    UnknownProposalError: 404,
    RunBusyError: 409,
    ProposalDecidedError: 409,
    FileExistsError: 409,
    LabelFileError: 500,
    MineFileError: 500,
}


def create_app(
    project: Project,
    *,
    user: str,
    static_dir: Path | None = STATIC_DIR,
    clock: Callable[[], datetime] = utc_now,
) -> FastAPI:
    """The UI server for ``project``.

    Args:
        project: The loaded ``hecklr.yaml``.
        user: Default labeler and reviewer name shown by the app.
        static_dir: The built app to serve at ``/``; ``None`` serves the API only (``--dev``).
        clock: Wall-clock source for label and decision timestamps (injected by tests).
    """
    app = FastAPI(title="Hecklr", version=__version__, docs_url="/api/docs", openapi_url=None)
    library = RunLibrary(project)
    labels = LabelBook(project.output_dir / LABELS_FILE)
    mine = MineFiles(project)
    stats = project.config.stats
    seed = stats.seed if stats.seed is not None else project.config.seed

    for error, status in _ERRORS.items():
        app.add_exception_handler(error, _handler(status))
    app.add_exception_handler(InvalidScenarioError, _invalid_scenario)

    @app.get("/api/meta")
    def meta() -> Meta:
        return Meta(
            version=__version__,
            project=project.config.agent.name,
            root=str(project.root),
            user=user,
            alpha=stats.alpha,
            min_effect=stats.min_effect,
            labels_file=str(labels.path),
            mine_dir=str(mine.directory),
            scenarios_dir=str(mine.scenarios_dir),
        )

    # --- runs -------------------------------------------------------------------------------

    @app.get("/api/runs")
    def runs() -> list[RunItem]:
        return [
            RunItem(
                info=RunInfo.of(s.run),
                counts=s.run.counts,
                planned=len(s.run.matrix),
                baseline_id=s.run.baseline_id,
            )
            for s in library.list_runs()
        ]

    @app.get("/api/runs/{run_id}/summary")
    def run_summary(run_id: str) -> RunSummary:
        analysis = library.analysis(run_id)
        return RunSummary(
            run_id=run_id,
            pass_rate=analysis.summary(PASS_RATE),
            cost_per_trial=analysis.summary(COST_PER_TRIAL),
            counts=analysis.counts,
            problems=analysis.problems,
        )

    @app.get("/api/runs/{run_id}")
    def run_analysis(run_id: str) -> Analysis:
        return library.analysis(run_id)

    @app.get("/api/compare")
    def compare(baseline: str, candidate: str) -> Analysis:
        if baseline == candidate:
            raise HTTPException(422, "pick two different runs to compare")
        return library.analysis(candidate, baseline)

    @app.get("/api/runs/{run_id}/trials/{trial_id}")
    def trial(run_id: str, trial_id: str) -> TrialDetail:
        stored = library.get(run_id)
        return trial_detail(
            library.results(run_id),
            library.analysis(run_id),
            trial_id,
            stored.directory,
            labels.all(),
        )

    @app.get("/api/runs/{run_id}/recordings/{path:path}")
    def recording(run_id: str, path: str) -> FileResponse:
        file, note = locate_recording(library.get(run_id).directory, path)
        if file is None:
            raise HTTPException(404, note)
        return FileResponse(file, media_type="audio/wav")

    # --- judge labeling ---------------------------------------------------------------------

    @app.get("/api/labels/queue")
    def queue(
        labeler: Annotated[str, Query(min_length=1)],
        run: Annotated[list[str] | None, Query()] = None,
        grader: str | None = None,
        limit: Annotated[int, Query(ge=1, le=500)] = DEFAULT_QUEUE_SIZE,
    ) -> LabelQueue:
        chosen = _judged_runs(library, run)
        titles = {
            s.scenario_id: s.title for r in chosen for s in library.analysis(r.run.id).scenarios
        }
        items, labeled = label_queue(
            chosen, titles, labels.all(), labeler=labeler, grader=grader, limit=limit, seed=seed
        )
        graders = sorted({g.grader for r in chosen for g in judge_grades(r)})
        return LabelQueue(
            runs=[r.run.id for r in chosen], graders=graders, labeled=labeled, items=items
        )

    @app.post("/api/labels", status_code=201)
    def add_label(body: LabelRequest) -> HumanLabel:
        grade = find_judge_grade(library.results(body.run_id), body.grade_id)
        return labels.add(
            grade, passed=body.passed, labeler=body.labeler, note=body.note, now=clock()
        )

    @app.get("/api/labels/agreement")
    def label_agreement() -> AgreementReport:
        recorded = labels.all()
        labeled_trials = {label.trial_id for label in recorded}
        grades = [
            grade
            for results in _readable_runs(library)
            for grade in judge_grades(results)
            if grade.trial_id in labeled_trials
        ]
        return agreement(grades, recorded, seed=seed, resamples=stats.bootstrap_resamples)

    # --- failure clusters and scenario approval --------------------------------------------

    @app.get("/api/clusters")
    def clusters() -> list[ClusterItem]:
        proposals = [s.proposal for s in mine.proposals()]
        return [
            ClusterItem(cluster=c, proposals=[p for p in proposals if p.cluster_id == c.id])
            for c in mine.clusters()
        ]

    @app.get("/api/clusters/{cluster_id}")
    def cluster(cluster_id: str) -> ClusterDetail:
        found = next((c for c in mine.clusters() if c.id == cluster_id), None)
        if found is None:
            raise HTTPException(404, f"no cluster {cluster_id!r}")
        exemplars = set(found.exemplar_trial_ids) | set(found.exemplar_conversation_ids)
        homes = _trial_homes(library, set(found.trial_ids))
        conversations = mine.conversations() if found.conversation_ids else {}
        members = [
            ClusterMember(id=t, exemplar=t in exemplars, run_id=homes.get(t))
            for t in found.trial_ids
        ] + [
            ClusterMember(id=c, exemplar=c in exemplars, conversation=conversations.get(c))
            for c in found.conversation_ids
        ]
        members.sort(key=lambda m: not m.exemplar)
        return ClusterDetail(
            cluster=found,
            members=members,
            proposals=[s.proposal for s in mine.proposals() if s.proposal.cluster_id == found.id],
        )

    @app.get("/api/proposals")
    def proposals(status: ProposalStatus | None = None) -> list[ProposalItem]:
        labels_by_cluster = {c.id: c.label for c in mine.clusters()}
        return [
            ProposalItem(
                proposal=s.proposal,
                yaml=scenario_to_yaml(s.proposal.scenario),
                cluster_label=labels_by_cluster.get(s.proposal.cluster_id),
                source=s.file.relative_to(mine.directory).as_posix(),
            )
            for s in mine.proposals()
            if status is None or s.proposal.status is status
        ]

    @app.post("/api/proposals/{proposal_id}/decision")
    def decide(proposal_id: str, body: Decision) -> DecisionResult:
        if body.decision == "reject":
            rejected = mine.reject(proposal_id, reviewer=body.reviewer, note=body.note, now=clock())
            return DecisionResult(proposal=rejected, written=None)
        approval = mine.approve(
            proposal_id, reviewer=body.reviewer, note=body.note, now=clock(), yaml_text=body.yaml
        )
        return DecisionResult(
            proposal=approval.proposal,
            written=str(approval.written),
            warnings=approval.warnings,
        )

    @app.get("/api/{rest:path}", include_in_schema=False)
    def unknown_api(rest: str) -> None:
        raise HTTPException(404, f"no API route /api/{rest}")

    if static_dir is not None:
        _serve_app(app, static_dir)
    return app


def _judged_runs(library: RunLibrary, refs: list[str] | None) -> list[RunResults]:
    """The runs to label: those named, else every readable run that has judge grades."""
    if refs:
        return [library.results(ref) for ref in refs]
    return [results for results in _readable_runs(library) if judge_grades(results)]


def _readable_runs(library: RunLibrary) -> list[RunResults]:
    readable = []
    for stored in library.list_runs():
        try:
            readable.append(library.results(stored.run.id))
        except RunBusyError:
            # A run still being written has no grades to label or agree with yet.
            continue
    return readable


def _trial_homes(library: RunLibrary, trial_ids: set[str]) -> dict[str, str]:
    """Trial id -> id of the stored run holding it, for the trials asked about."""
    homes: dict[str, str] = {}
    if not trial_ids:
        return homes
    for results in _readable_runs(library):
        for trial in results.trials:
            if trial.id in trial_ids:
                homes[trial.id] = results.run.id
    return homes


def _handler(status: int) -> Callable[[Request, Exception], JSONResponse]:
    def handle(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status)

    return handle


def _invalid_scenario(_: Request, exc: Exception) -> JSONResponse:
    problems = exc.problems if isinstance(exc, InvalidScenarioError) else [str(exc)]
    return JSONResponse({"detail": "the scenario is invalid", "problems": problems}, 422)


def _serve_app(app: FastAPI, static_dir: Path) -> None:
    """Serve the built single-page app: real files as they are, every other path its index."""
    index = static_dir / "index.html"
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/{rest:path}", include_in_schema=False)
    def spa(rest: str) -> FileResponse:
        candidate = (static_dir / rest).resolve()
        if rest and candidate.is_relative_to(static_dir.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(index, headers={"cache-control": "no-cache"})
