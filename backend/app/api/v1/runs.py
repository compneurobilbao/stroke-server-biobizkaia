"""Runs: create, status, step timeline and logs, provenance, artefacts, cancel, resume.

Run creation and the two actions call the orchestrator package inside the request
transaction (``orchestrator.resolution.create_run``, ``orchestrator.actions``), commit,
and only then enqueue or revoke the Celery task, as those modules document.
"""

from __future__ import annotations

import json
import logging
import mimetypes
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    Orchestration,
    RunQueue,
    audit_event,
    get_orchestration,
    get_run_queue,
    load_case,
    load_run,
    storage_for,
)
from app.api.v1.errors import V1Error, v1_error
from app.api.v1.pipelines import find_pipeline
from app.api.v1.quota import storage_used
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.models.artifact import Artifact, Report
from app.models.case import InputFile
from app.models.pipeline_run import ACTIVE_STATUSES, PipelineRun, RunSlot, StepRun
from app.models.user import User
from app.schemas.api_v1 import (
    ArtifactV1Read,
    ReportV1Read,
    RunActionResponse,
    RunCreate,
    RunDetailRead,
    StepAttemptRead,
)
from app.schemas.run import PipelineRunRead
from app.services.auth import client_ip, get_current_user
from orchestrator.actions import ActionRefused, cancel_run, resume_run
from orchestrator.executors.base import Accounting
from orchestrator.resolution import RunRefused, create_run

log = logging.getLogger(__name__)
router = APIRouter(tags=["runs"])

_UNPROCESSABLE = {
    "unknown_slot",
    "implementation_not_enabled",
    "input_type_mismatch",
    "no_matching_input_set",
    "input_not_in_case",
}
_ACTION_STATUS = {"run_not_found": 404, "legacy_run": 404}


def _attempt(a: StepRun, slot_name: str) -> StepAttemptRead:
    data = {
        name: getattr(a, name)
        for name in StepAttemptRead.model_fields
        if hasattr(a, name)
    }
    data["slot"] = slot_name
    data["log_available"] = a.log_path is not None
    return StepAttemptRead(**data)


def _detail_read(db: Session, run: PipelineRun) -> RunDetailRead:
    names = {s.id: s.slot for s in run.slots}
    attempts = db.scalars(
        select(StepRun)
        .where(StepRun.pipeline_run_id == run.id)
        .order_by(StepRun.queued_at, StepRun.attempt)
    ).all()
    base = PipelineRunRead.model_validate(run).model_dump()
    return RunDetailRead(
        **base,
        cancel_requested=run.cancel_requested_at is not None,
        inputs=[{"role": i.role, "input_file_id": i.input_file_id} for i in run.inputs],
        slots=[_slot(s) for s in run.slots],
        steps=[_attempt(a, names[a.run_slot_id]) for a in attempts],
    )


def _slot(s: RunSlot):
    from app.schemas.run import RunSlotRead

    return RunSlotRead.model_validate(s)


def _enqueue(db: Session, run: PipelineRun, queue: RunQueue) -> None:
    """Enqueue the orchestrator task after commit. A broker failure leaves the run queued;
    ``recover_pipeline_runs`` re-enqueues it once its heartbeat is stale."""
    try:
        task_id = queue.enqueue(run.id)
    except Exception:
        log.exception("Could not enqueue run %s", run.id)
        return
    if task_id:
        run.orchestrator_task_id = task_id
        db.commit()


@router.post("/cases/{case_id}/runs", response_model=RunDetailRead, status_code=201)
def create_case_run(
    case_id: UUID,
    body: RunCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Orchestration = Depends(get_orchestration),
    queue: RunQueue = Depends(get_run_queue),
) -> RunDetailRead:
    """Resolve the pipeline against the case's inputs, validate the per-slot choices
    (422 when a choice is not enabled), store the run as ``queued`` and enqueue it."""
    case = load_case(db, case_id, current_user, "run.create")
    pipeline = find_pipeline(orch, body.pipeline_id, body.pipeline_version)
    files = {
        f.role: f
        for f in db.scalars(select(InputFile).where(InputFile.case_id == case.id))
    }
    if body.input_roles is not None:
        missing = set(body.input_roles) - set(files)
        if missing:
            raise v1_error(
                422, "input_not_found", f"The case has no input: {sorted(missing)}"
            )
        files = {r: files[r] for r in body.input_roles}
    if not files:
        raise v1_error(409, "no_inputs", "The case has no inputs")
    active = db.scalar(
        select(PipelineRun.id)
        .where(PipelineRun.case_id == case.id, PipelineRun.status.in_(ACTIVE_STATUSES))
        .limit(1)
    )
    if active is not None:
        raise v1_error(409, "active_run_exists", "Another run of this case is active")
    owner = db.get(User, case.owner_user_id)
    if (
        owner.storage_quota_bytes is not None
        and storage_used(db, owner.id) >= owner.storage_quota_bytes
    ):
        raise v1_error(
            413, "storage_quota_exceeded", "The case owner has no storage quota left"
        )
    try:
        run = create_run(
            db,
            case=case,
            pipeline=pipeline,
            inputs=files,
            created_by_user_id=current_user.id,
            steps=orch.steps,
            bundles=orch.bundles,
            accounting=Accounting(submitted_by=f"user:{current_user.id}"),
            platform_version=orch.platform_version,
            requested_implementations=body.implementations,
            seed=body.seed,
        )
    except RunRefused as exc:
        db.rollback()
        status = 422 if exc.code in _UNPROCESSABLE else 409
        raise V1Error(status, exc.code, str(exc), exc.slot) from None
    audit_event(
        db,
        "run_created",
        current_user,
        case_id=case.id,
        run_id=run.id,
        details={"pipeline_id": pipeline.id, "pipeline_version": pipeline.version},
        ip_address=client_ip(request),
    )
    try:
        db.commit()
    except IntegrityError:  # lost a race with another run of the same case
        db.rollback()
        raise v1_error(
            409, "active_run_exists", "Another run of this case is active"
        ) from None
    _enqueue(db, run, queue)
    return _detail_read(db, run)


@router.get("/cases/{case_id}/runs", response_model=list[PipelineRunRead])
def list_case_runs(
    case_id: UUID,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[PipelineRun]:
    case = load_case(db, case_id, current_user, "run.read")
    return list(
        db.scalars(
            select(PipelineRun)
            .where(PipelineRun.case_id == case.id)
            .order_by(PipelineRun.created_at.desc(), PipelineRun.id)
            .limit(limit)
            .offset(offset)
        )
    )


@router.get("/runs/{run_id}", response_model=RunDetailRead)
def get_run(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> RunDetailRead:
    """Status, slot choices (implementation, image digest, parameters, bundles) and the
    step timeline (one entry per attempt)."""
    run, _case = load_run(db, run_id, current_user, "run.read")
    return _detail_read(db, run)


@router.get("/runs/{run_id}/steps", response_model=list[StepAttemptRead])
def list_steps(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[StepAttemptRead]:
    run, _case = load_run(db, run_id, current_user, "run.read")
    return _detail_read(db, run).steps


@router.get("/runs/{run_id}/steps/{step_run_id}/log", response_class=PlainTextResponse)
def get_step_log(
    run_id: UUID,
    step_run_id: UUID,
    tail_bytes: int = Query(262144, ge=1, le=4194304),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> PlainTextResponse:
    """The last ``tail_bytes`` of the attempt's log. ``X-Log-Truncated: true`` when the
    start is missing (cut here or by the orchestrator)."""
    run, _case = load_run(db, run_id, current_user, "run.logs")
    step = db.get(StepRun, step_run_id)
    if step is None or step.pipeline_run_id != run.id:
        raise v1_error(404, "step_run_not_found", "Step attempt not found")
    path = None
    if step.log_path:
        try:
            path = storage_for(settings).resolve_relative(step.log_path)
        except ValueError:
            path = None
    if path is None or not path.is_file():
        raise v1_error(404, "log_not_available", "No log is available for this attempt")
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size > tail_bytes:
            handle.seek(size - tail_bytes)
        data = handle.read(tail_bytes)
    truncated = size > tail_bytes or step.log_truncated
    return PlainTextResponse(
        data.decode("utf-8", errors="replace"),
        headers={"X-Log-Truncated": "true" if truncated else "false"},
    )


@router.get("/runs/{run_id}/provenance")
def get_provenance(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    run, _case = load_run(db, run_id, current_user, "run.read")
    path = None
    if run.provenance_path:
        try:
            path = storage_for(settings).resolve_relative(run.provenance_path)
        except ValueError:
            path = None
    if path is None or not path.is_file():
        raise v1_error(404, "provenance_not_available", "No provenance record yet")
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


# ------------------------------------------------------------- artefacts, reports


def _artifact_read(a: Artifact) -> ArtifactV1Read:
    data = ArtifactV1Read.model_fields
    values = {name: getattr(a, name) for name in data if hasattr(a, name)}
    values["filename"] = Path(a.storage_path).name
    return ArtifactV1Read(**values)


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactV1Read])
def list_artifacts(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ArtifactV1Read]:
    run, _case = load_run(db, run_id, current_user, "artifact.download")
    rows = db.scalars(
        select(Artifact)
        .where(Artifact.pipeline_run_id == run.id)
        .order_by(Artifact.created_at, Artifact.port)
    )
    return [_artifact_read(a) for a in rows]


def _send(settings: Settings, artifact: Artifact) -> FileResponse:
    try:
        path = storage_for(settings).resolve_relative(artifact.storage_path)
    except ValueError:
        path = None
    if path is None or not path.is_file():
        raise v1_error(
            404, "file_missing_in_storage", "The file does not exist in storage"
        )
    media = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.name.endswith(".nii.gz"):
        media = "application/gzip"
    return FileResponse(path, filename=path.name, media_type=media)


@router.get("/runs/{run_id}/artifacts/{artifact_id}/download")
def download_artifact(
    run_id: UUID,
    artifact_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    run, _case = load_run(db, run_id, current_user, "artifact.download")
    artifact = db.get(Artifact, artifact_id)
    if artifact is None or artifact.pipeline_run_id != run.id:
        raise v1_error(404, "artifact_not_found", "Artefact not found")
    return _send(settings, artifact)


@router.get("/runs/{run_id}/reports", response_model=list[ReportV1Read])
def list_reports(
    run_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ReportV1Read]:
    run, _case = load_run(db, run_id, current_user, "report.download")
    out = []
    for report in db.scalars(select(Report).where(Report.pipeline_run_id == run.id)):
        values = {
            name: getattr(report, name)
            for name in ReportV1Read.model_fields
            if hasattr(report, name)
        }
        values["pdf_filename"] = Path(report.pdf_artifact.storage_path).name
        out.append(ReportV1Read(**values))
    return out


@router.get("/runs/{run_id}/reports/{report_id}/pdf")
def download_report_pdf(
    run_id: UUID,
    report_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    run, _case = load_run(db, run_id, current_user, "report.download")
    report = db.get(Report, report_id)
    if report is None or report.pipeline_run_id != run.id:
        raise v1_error(404, "report_not_found", "Report not found")
    return _send(settings, report.pdf_artifact)


# ----------------------------------------------------------------------- actions


def _refused(exc: ActionRefused) -> V1Error:
    return V1Error(_ACTION_STATUS.get(exc.code, 409), exc.code, str(exc))


@router.post("/runs/{run_id}/cancel", response_model=RunActionResponse)
def cancel(
    run_id: UUID,
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    queue: RunQueue = Depends(get_run_queue),
) -> RunActionResponse:
    """Idempotent (docs/data-model.md 7.3): 200 when the run is canceled, 202 while a
    running run is asked to stop, 409 ``run_not_active`` for a finished run."""
    run, _case = load_run(db, run_id, current_user, "run.cancel")
    task_id = run.orchestrator_task_id
    try:
        result = cancel_run(db, run.id, current_user.id)
    except ActionRefused as exc:
        db.rollback()
        raise _refused(exc) from None
    db.commit()
    if result.revoke_task and task_id:
        try:
            queue.revoke(task_id)
        except Exception:
            log.exception("Could not revoke task of run %s", run.id)
    response.status_code = 200 if result.status == "canceled" else 202
    return RunActionResponse(
        run_id=run.id,
        status=result.status,
        cancel_requested=result.cancel_requested,
        changed=result.changed,
    )


def _resume(
    run_id: UUID,
    response: Response,
    current_user: User,
    db: Session,
    orch: Orchestration,
    queue: RunQueue,
) -> RunActionResponse:
    run, _case = load_run(db, run_id, current_user, "run.resume")
    try:
        result = resume_run(
            db,
            run.id,
            current_user.id,
            pipelines=orch.pipelines,
            steps=orch.steps,
            bundles=orch.bundles,
        )
    except ActionRefused as exc:
        db.rollback()
        raise _refused(exc) from None
    except IntegrityError:
        db.rollback()
        raise v1_error(
            409, "active_run_exists", "Another run of this case is active"
        ) from None
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise v1_error(
            409, "active_run_exists", "Another run of this case is active"
        ) from None
    if result.enqueue:
        db.refresh(run)
        _enqueue(db, run, queue)
    response.status_code = 202 if result.enqueue else 200
    return RunActionResponse(
        run_id=run.id,
        status=result.status,
        cancel_requested=result.cancel_requested,
        changed=result.changed,
    )


@router.post("/runs/{run_id}/resume", response_model=RunActionResponse)
def resume(
    run_id: UUID,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Orchestration = Depends(get_orchestration),
    queue: RunQueue = Depends(get_run_queue),
) -> RunActionResponse:
    """Resume a failed or canceled run from its failed step: succeeded slots are kept,
    the seed, choices, parameters and bundles stay those of the run. 202 when queued,
    200 when the run is already active; refusals are 409 with the codes of data-model.md 7.3."""
    return _resume(run_id, response, current_user, db, orch, queue)


@router.post("/runs/{run_id}/retry", response_model=RunActionResponse)
def retry(
    run_id: UUID,
    response: Response,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Orchestration = Depends(get_orchestration),
    queue: RunQueue = Depends(get_run_queue),
) -> RunActionResponse:
    """Alias of ``resume`` ("retry from the failed step" is the same operation)."""
    return _resume(run_id, response, current_user, db, orch, queue)
