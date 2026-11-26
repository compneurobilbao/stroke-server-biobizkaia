"""Legacy study endpoints, aliases over the case model since migration 0012.

docs/data-model.md 10.4: every path keeps its method, request and response schema and
its status codes; the data now lives in ``cases``, ``input_files``, ``pipeline_runs``,
``step_runs``, ``artifacts`` and ``reports`` (legacy study id = case id). ``studies``
and ``processing_jobs`` are frozen and never written. Legacy uploads still run on the
legacy worker task (``worker.tasks.process_study``, cancellation by Celery revoke plus
SIGTERM). Only cases of the ``legacy`` profile are served here (back-filled studies and
uploads through this alias); cases created through /api/v1 answer 404, just as v1 hides
legacy cases.
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, api_error
from app.db.session import get_db
from app.models.artifact import Artifact, Report
from app.models.case import LEGACY_UPLOAD, Case, DeploymentProfile, InputFile
from app.models.pipeline_run import (
    ACTIVE_STATUSES,
    LEGACY_PIPELINE_ID,
    LEGACY_SLOT,
    PipelineRun,
    RunInput,
    RunSlot,
    StepRun,
)
from app.models.share_link import ShareLink
from app.models.study import ClinicalReviewStatus
from app.models.user import User
from app.schemas.study import (
    ClinicalReviewUpdate,
    StudyActionResponse,
    StudyDetailRead,
    StudyLogEntry,
    StudyLogsRead,
    StudyRead,
    StudyStatusRead,
    UploadResponse,
)
from app.services import legacy_studies as legacy
from app.services.audit import record_event
from app.services.auth import (
    client_ip,
    get_current_user,
)
from app.services.bids import prepare_single_subject_t1w_bids
from app.services.quota import storage_used
from app.services.security import (
    resolve_bids_subject_id,
    sanitize_filename,
    validate_nifti_gz_filename,
    validate_upload,
)
from app.services.storage import LocalStudyStorage, UploadTooLargeError
from worker.tasks import process_study

router = APIRouter()
log = logging.getLogger(__name__)

CANCEL_REQUESTED_MESSAGE = "Cancellation requested by the user"


def _actor(request: Request, user: User) -> dict:
    return {
        "actor": user.email,
        "actor_user_id": user.id,
        "ip_address": client_ip(request),
    }


def _new_case_id(db: Session) -> UUID:
    """A case id whose subject label (first 12 hex characters) is not taken."""
    while True:
        case_id = uuid4()
        taken = db.scalar(
            select(Case.id).where(Case.subject_label == case_id.hex[:12])
        )
        if taken is None:
            return case_id


def _lock_owner_quota(db: Session, user: User, additional: int) -> None:
    """Quota check under the owner's row lock (same lock as the v1 uploads)."""
    if user.storage_quota_bytes is None:
        return
    db.execute(
        select(User.id).where(User.id == user.id).with_for_update()
    ).all()
    if storage_used(db, user.id) + additional > user.storage_quota_bytes:
        raise api_error(ErrorCode.STORAGE_QUOTA_EXCEEDED)


@router.post(
    "/studies/upload",
    response_model=UploadResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_study(
    request: Request,
    file: UploadFile = File(...),
    bids_subject_id: str | None = Form(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UploadResponse:
    """A case with one ``legacy_upload`` input file and one queued ``legacy`` run."""
    processor_backend = settings.processor_backend.strip().lower()
    if processor_backend == "compneuro":
        safe_filename = sanitize_filename(file.filename or "")
        validate_nifti_gz_filename(safe_filename)
    else:
        safe_filename = validate_upload(file, settings.allowed_extension_list)
    study_id = _new_case_id(db)
    resolved_bids_subject_id = (
        resolve_bids_subject_id(bids_subject_id, study_id)
        if processor_backend == "compneuro" or bids_subject_id
        else None
    )
    storage = LocalStudyStorage(settings.storage_root)

    try:
        stored_path, file_size, checksum = storage.save_upload(
            study_id, file, safe_filename, max_bytes=settings.max_upload_size_bytes
        )
    except UploadTooLargeError:
        storage.remove_study(study_id)
        raise api_error(ErrorCode.UPLOAD_TOO_LARGE) from None
    try:
        _lock_owner_quota(db, current_user, file_size)
    except Exception:
        db.rollback()
        storage.remove_study(study_id)
        raise

    bids_paths = None
    if processor_backend == "compneuro" and resolved_bids_subject_id:
        try:
            bids_paths = prepare_single_subject_t1w_bids(
                storage, study_id, stored_path, resolved_bids_subject_id
            )
        except Exception as exc:
            db.rollback()
            storage.remove_study(study_id)
            raise api_error(ErrorCode.BIDS_PREPARATION_FAILED) from exc

    now = legacy.utcnow()
    rel = lambda path: legacy.relative(storage, path)  # noqa: E731
    case = Case(
        id=study_id,
        owner_user_id=current_user.id,
        subject_label=study_id.hex[:12],
        # Legacy uploads stay in the legacy world for this release: hidden from /api/v1
        # like the back-filled studies (docs/data-model.md 10.4 and section 11).
        deployment_profile=DeploymentProfile.legacy.value,
        created_at=now,
        updated_at=now,
        legacy_attributes={
            "original_filename": file.filename or safe_filename,
            "bids_subject_id": resolved_bids_subject_id,
            "processor_backend": processor_backend,
            "processor_name": settings.processor_name,
            "processor_version": settings.processor_version,
            "container_image": settings.compneuro_container_image
            if processor_backend == "compneuro"
            else None,
            "processing_warnings": None,
            "bids_path": rel(bids_paths.bids_project_dir) if bids_paths else None,
            "preproc_output_path": rel(storage.preproc_output_dir(study_id))
            if processor_backend == "compneuro"
            else None,
            "rendered_png_dir": None,
            "output_path": rel(storage.output_dir(study_id)),
        },
    )
    input_file = InputFile(
        id=legacy.derive(study_id, "input"),
        case_id=study_id,
        role=LEGACY_UPLOAD,
        artefact_type=LEGACY_UPLOAD,
        storage_path=rel(stored_path),
        sha256=checksum,
        size_bytes=file_size,
        uploaded_by_user_id=current_user.id,
        created_at=now,
    )
    run = PipelineRun(
        id=study_id,
        case_id=study_id,
        created_by_user_id=current_user.id,
        pipeline_id=LEGACY_PIPELINE_ID,
        pipeline_version="0.0.0",
        platform_version="legacy",
        artefact_types_version="legacy",
        input_set="legacy-single-file",
        seed=0,
        status="queued",
        requested_implementations={},
        accounting_submitted_by=f"user:{current_user.id}",
        accounting_submit_as="service-account",
        created_at=now,
        updated_at=now,
        legacy_clinical_review_status=ClinicalReviewStatus.technical_only.value,
    )
    implementation = legacy.kebab(processor_backend)
    slot = RunSlot(
        id=legacy.derive(study_id, "slot/legacy"),
        pipeline_run_id=study_id,
        slot=LEGACY_SLOT,
        position=0,
        optional=False,
        status="selected",
        implementation_id=implementation,
        step_name=f"legacy-{implementation}",
        step_version="0.0.0",
        stub=processor_backend == "dummy",
        chosen_by="deployment_default",
        effective_parameters={},
    )
    step = StepRun(
        id=uuid4(),
        pipeline_run_id=study_id,
        run_slot_id=slot.id,
        attempt=1,
        trigger="initial",
        status="queued",
        executor="subprocess",
        runtime="none",
        queued_at=now,
    )
    db.add(case)
    db.flush()
    db.add_all([input_file, run])
    db.flush()
    db.add_all(
        [RunInput(pipeline_run_id=run.id, role=LEGACY_UPLOAD, input_file_id=input_file.id), slot]
    )
    db.flush()
    db.add(step)
    db.flush()
    record_event(
        db,
        "study_uploaded",
        details={
            "filename": safe_filename,
            "processor_backend": processor_backend,
            "bids_subject_id": resolved_bids_subject_id,
        },
        actor=current_user.email,
        actor_user_id=current_user.id,
        ip_address=request.client.host if request.client else None,
        case_id=study_id,
        pipeline_run_id=run.id,
    )
    db.commit()

    storage.write_metadata(
        study_id,
        {
            "study_id": str(study_id),
            "owner_user_id": str(current_user.id),
            "original_filename": file.filename,
            "stored_filename": safe_filename,
            "checksum_sha256": checksum,
            "file_size": file_size,
            "processor_backend": processor_backend,
            "bids_subject_id": resolved_bids_subject_id,
            "bids_path": str(bids_paths.bids_project_dir) if bids_paths else None,
        },
    )

    _enqueue_attempt(db, study_id, step.id)
    return UploadResponse(
        id=study_id,
        status="queued",
        message="Study received and queued for processing",
        bids_subject_id=resolved_bids_subject_id,
    )


def _enqueue_attempt(db: Session, case_id: UUID, step_id: UUID) -> None:
    """Enqueue the legacy worker task for one attempt and remember its task id."""
    async_result = process_study.delay(str(case_id), str(step_id))
    task_id = getattr(async_result, "id", None)
    if task_id:
        db.execute(
            update(StepRun)
            .where(StepRun.id == step_id)
            .values(orchestrator_task_id=task_id)
            .execution_options(synchronize_session=False)
        )
        db.commit()


@router.get("/studies", response_model=list[StudyRead])
def list_studies(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[StudyRead]:
    return legacy.study_reads(db, legacy.visible_cases(db, current_user))


@router.get("/studies/{study_id}", response_model=StudyRead)
def get_study(
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudyRead:
    return legacy.study_read(db, legacy.visible_case(db, study_id, current_user))


@router.get("/studies/{study_id}/detail", response_model=StudyDetailRead)
def get_study_detail(
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StudyDetailRead:
    case = legacy.visible_case(db, study_id, current_user)
    run = legacy.latest_run(db, case.id)
    storage = LocalStudyStorage(settings.storage_root)
    steps = legacy.run_steps(db, run.id) if run else []
    return StudyDetailRead(
        **legacy.study_read(db, case).model_dump(),
        jobs=[legacy.job_read(step, case.id, storage) for step in steps],
    )


@router.get("/studies/{study_id}/status", response_model=StudyStatusRead)
def get_study_status(
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudyStatusRead:
    return legacy.status_read(db, legacy.visible_case(db, study_id, current_user))


@router.patch("/studies/{study_id}/clinical-review", response_model=StudyRead)
def update_clinical_review_status(
    request: Request,
    study_id: UUID,
    payload: ClinicalReviewUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudyRead:
    """``legacy_clinical_review_status`` of the latest run."""
    case = legacy.visible_case(db, study_id, current_user)
    allowed_statuses = {item.value for item in ClinicalReviewStatus}
    if payload.clinical_review_status not in allowed_statuses:
        raise api_error(ErrorCode.INVALID_CLINICAL_REVIEW_STATUS)
    run = legacy.latest_run(db, case.id)
    if (
        payload.clinical_review_status != ClinicalReviewStatus.technical_only.value
        and legacy.study_status(run) != "completed"
    ):
        raise api_error(ErrorCode.CLINICAL_REVIEW_REQUIRES_COMPLETED)
    if run is not None:
        run.legacy_clinical_review_status = payload.clinical_review_status
        run.updated_at = legacy.utcnow()
    record_event(
        db,
        "study_clinical_review_updated",
        details={"clinical_review_status": payload.clinical_review_status},
        **_actor(request, current_user),
        case_id=case.id,
        pipeline_run_id=run.id if run else None,
    )
    db.commit()
    db.refresh(case)
    return legacy.study_read(db, case)


def _completed_report_pdf(
    db: Session, case: Case, storage: LocalStudyStorage
) -> Path:
    """PDF of the case's current report: the report of the latest run, which must have
    succeeded (``completed``)."""
    run = legacy.latest_run(db, case.id)
    report = legacy.current_report(db, run)
    if legacy.study_status(run) != "completed" or report is None:
        raise api_error(ErrorCode.PDF_NOT_READY)
    pdf_path = legacy.report_pdf_path(db, storage, report)
    if (
        pdf_path is None
        or not pdf_path.is_file()
        or pdf_path.suffix.lower() != ".pdf"
    ):
        raise api_error(ErrorCode.PDF_MISSING_IN_STORAGE)
    return pdf_path


@router.get("/studies/{study_id}/download")
def download_pdf(
    request: Request,
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    case = legacy.visible_case(db, study_id, current_user)
    pdf_path = _completed_report_pdf(
        db, case, LocalStudyStorage(settings.storage_root)
    )
    record_event(
        db,
        "study_downloaded_pdf",
        **_actor(request, current_user),
        case_id=case.id,
    )
    db.commit()
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"technical-report-{case.id}.pdf",
    )


@router.get("/studies/{study_id}/download/pdf")
def download_technical_pdf(
    request: Request,
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    return download_pdf(request, study_id, current_user, db, settings)


@router.get("/studies/{study_id}/download/zip")
def download_outputs_zip(
    request: Request,
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """``legacy_outputs_zip`` artefact of the latest run."""
    case = legacy.visible_case(db, study_id, current_user)
    run = legacy.latest_run(db, case.id)
    artifact = legacy.outputs_zip(db, run)
    if legacy.study_status(run) != "completed" or artifact is None:
        raise api_error(ErrorCode.ZIP_NOT_READY)
    zip_path = legacy.absolute(
        LocalStudyStorage(settings.storage_root), artifact.storage_path
    )
    if zip_path is None or not zip_path.is_file() or zip_path.suffix.lower() != ".zip":
        raise api_error(ErrorCode.ZIP_MISSING_IN_STORAGE)
    record_event(
        db,
        "study_downloaded_zip",
        **_actor(request, current_user),
        case_id=case.id,
    )
    db.commit()
    return FileResponse(
        zip_path, media_type="application/zip", filename=f"outputs-{case.id}.zip"
    )


@router.get("/studies/{study_id}/logs", response_model=StudyLogsRead)
def get_study_logs(
    study_id: UUID,
    lines: int = Query(default=200, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StudyLogsRead:
    case = legacy.visible_case(db, study_id, current_user)
    storage = LocalStudyStorage(settings.storage_root)
    logs = []
    for name in ["processor.log", "rendering.log"]:
        path = storage.logs_dir(case.id) / name
        if path.exists() and path.is_file():
            content, truncated = _read_last_lines(path, lines)
            logs.append(StudyLogEntry(name=name, content=content, truncated=truncated))
    return StudyLogsRead(study_id=case.id, logs=logs)


# ------------------------------------------------------------------- run actions


def _revoke(task_id: str | None, **kwargs) -> None:
    if task_id:
        process_study.app.control.revoke(task_id, **kwargs)


@router.post("/studies/{study_id}/cancel", response_model=StudyActionResponse)
def cancel_study_processing(
    request: Request,
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudyActionResponse:
    """Cancel of the latest run (docs/data-model.md 7.3), with the legacy mechanism.

    A queued run is canceled at once (the Celery task is revoked). A processing run gets
    ``cancel_requested_at`` on the run and on its live attempt and the worker task is
    revoked with SIGTERM, which the worker turns into a cancellation because the flag is
    set. A cancel that finds the run queued but loses the race against the worker (the
    run became ``running`` meanwhile) continues as a processing cancel, as the legacy
    endpoint did; it never answers 409 for that race.
    """
    case = legacy.visible_case(db, study_id, current_user)
    run = legacy.latest_run(db, case.id)
    current = legacy.study_status(run)
    if current not in {"queued", "processing"}:
        raise api_error(ErrorCode.CANCEL_INVALID_STATE)
    job = legacy.latest_step(db, run)
    now = legacy.utcnow()

    if current == "queued":
        if job and job.orchestrator_task_id:
            _revoke(job.orchestrator_task_id, terminate=False)
        canceled = db.execute(
            update(PipelineRun)
            .where(PipelineRun.id == run.id, PipelineRun.status == "queued")
            .values(
                status="canceled",
                finished_at=now,
                failure_message="Canceled by the user",
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if canceled == 1:
            db.execute(
                update(StepRun)
                .where(StepRun.pipeline_run_id == run.id, StepRun.status == "queued")
                .values(
                    status="canceled",
                    outcome="canceled",
                    finished_at=now,
                    failure_message="Canceled before processing started",
                )
                .execution_options(synchronize_session=False)
            )
            record_event(
                db,
                "study_canceled",
                details={"job_id": str(job.id) if job else None},
                **_actor(request, current_user),
                case_id=case.id,
                pipeline_run_id=run.id,
            )
            db.commit()
            return StudyActionResponse(
                id=case.id, status="canceled", message="Processing canceled"
            )
        # The worker started the run meanwhile: cancel it as a processing run.
        db.rollback()
        db.expire_all()
        run = legacy.latest_run(db, case.id)
        if legacy.study_status(run) != "processing":
            raise api_error(ErrorCode.CANCEL_INVALID_STATE)
        job = legacy.latest_step(db, run)

    if not job or not job.orchestrator_task_id:
        raise api_error(ErrorCode.CANCEL_WITHOUT_TASK)
    updated_runs = db.execute(
        update(PipelineRun)
        .where(PipelineRun.id == run.id, PipelineRun.status == "running")
        .values(
            failure_message=CANCEL_REQUESTED_MESSAGE,
            cancel_requested_at=now,
            cancel_requested_by_user_id=current_user.id,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    updated_jobs = db.execute(
        update(StepRun)
        .where(StepRun.id == job.id, StepRun.status == "running")
        .values(failure_message=CANCEL_REQUESTED_MESSAGE, cancel_requested_at=now)
        .execution_options(synchronize_session=False)
    ).rowcount
    if updated_runs != 1 or updated_jobs != 1:
        db.rollback()
        raise api_error(ErrorCode.PROCESSING_NOT_ACTIVE)
    record_event(
        db,
        "study_cancellation_requested",
        details={"job_id": str(job.id)},
        **_actor(request, current_user),
        case_id=case.id,
        pipeline_run_id=run.id,
    )
    db.commit()
    _revoke(job.orchestrator_task_id, terminate=True, signal="SIGTERM")
    return StudyActionResponse(
        id=case.id, status="processing", message="Cancellation requested"
    )


@router.post("/studies/{study_id}/retry", response_model=StudyActionResponse)
def retry_failed_study(
    request: Request,
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudyActionResponse:
    """Resume of the latest run (7.3): a new attempt of its single ``legacy`` slot
    (trigger ``user_resume``), queued on the legacy worker task."""
    case = legacy.visible_case(db, study_id, current_user)
    run = legacy.latest_run(db, case.id)
    if legacy.study_status(run) != "failed":
        raise api_error(ErrorCode.RETRY_INVALID_STATE)
    slot = db.scalar(
        select(RunSlot).where(
            RunSlot.pipeline_run_id == run.id, RunSlot.slot == LEGACY_SLOT
        )
    )
    if slot is None:
        raise api_error(ErrorCode.RETRY_INVALID_STATE)
    now = legacy.utcnow()
    requeued = db.execute(
        update(PipelineRun)
        .where(PipelineRun.id == run.id, PipelineRun.status == "failed")
        .values(
            status="queued",
            started_at=None,
            finished_at=None,
            failure_code=None,
            failure_message=None,
            failed_step_run_id=None,
            cancel_requested_at=None,
            cancel_requested_by_user_id=None,
            resume_count=PipelineRun.resume_count + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    if requeued != 1:
        db.rollback()
        raise api_error(ErrorCode.RETRY_INVALID_STATE)
    # An attempt left live by a crashed worker would block the new one
    # (uq_step_runs_one_active); it is dead, close it as the back-fill does.
    db.execute(
        update(StepRun)
        .where(StepRun.run_slot_id == slot.id, StepRun.status.in_(ACTIVE_STATUSES))
        .values(
            status="failed",
            outcome="failed",
            failure_code=legacy.STALE_ATTEMPT_CODE,
            finished_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    attempt = (
        db.scalar(
            select(func.max(StepRun.attempt)).where(StepRun.run_slot_id == slot.id)
        )
        or 0
    ) + 1
    retry_count = attempt - 1
    # The legacy retry cleared the PDF, the ZIP and the warnings of the study: drop the
    # run's legacy report and artefacts (a later success writes them again).
    _drop_legacy_outputs(db, run.id)
    attrs = dict(case.legacy_attributes or {})
    if attrs.get("processing_warnings") is not None:
        attrs["processing_warnings"] = None
        case.legacy_attributes = attrs
    step = StepRun(
        id=uuid4(),
        pipeline_run_id=run.id,
        run_slot_id=slot.id,
        attempt=attempt,
        trigger="user_resume",
        status="queued",
        executor="subprocess",
        runtime="none",
        queued_at=now,
    )
    db.add(step)
    db.flush()
    record_event(
        db,
        "study_retry_queued",
        details={"retry_count": retry_count},
        **_actor(request, current_user),
        case_id=case.id,
        pipeline_run_id=run.id,
    )
    db.commit()
    _enqueue_attempt(db, case.id, step.id)
    return StudyActionResponse(id=case.id, status="queued", message="Job requeued")


def _drop_legacy_outputs(db: Session, run_id: UUID) -> None:
    report_ids = list(
        db.scalars(select(Report.id).where(Report.pipeline_run_id == run_id))
    )
    if report_ids:
        # Only reports of succeeded runs can be shared, and a succeeded run is never
        # retried; unbinding is a guard, the link would serve the current report.
        db.execute(
            update(ShareLink)
            .where(ShareLink.report_id.in_(report_ids))
            .values(report_id=None)
            .execution_options(synchronize_session=False)
        )
        db.execute(
            delete(Report)
            .where(Report.id.in_(report_ids))
            .execution_options(synchronize_session=False)
        )
    db.execute(
        delete(Artifact)
        .where(Artifact.pipeline_run_id == run_id, Artifact.origin == "legacy")
        .execution_options(synchronize_session=False)
    )


@router.delete("/studies/{study_id}", response_model=StudyActionResponse)
def delete_study(
    request: Request,
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StudyActionResponse:
    """Case deletion: soft delete plus removal of the case directory, as before. A
    queued run is canceled first; a processing run refuses the deletion."""
    case = legacy.visible_case(db, study_id, current_user)
    run = legacy.latest_run(db, case.id)
    current = legacy.study_status(run)
    if current == "processing":
        raise api_error(ErrorCode.DELETE_WHILE_PROCESSING)
    now = legacy.utcnow()
    if current == "queued":
        job = legacy.latest_step(db, run)
        task_id = (job.orchestrator_task_id if job else None) or run.orchestrator_task_id
        if task_id:
            _revoke(task_id)
        canceled = db.execute(
            update(PipelineRun)
            .where(PipelineRun.id == run.id, PipelineRun.status == "queued")
            .values(status="canceled", finished_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        ).rowcount
        if canceled != 1:
            db.rollback()
            raise api_error(ErrorCode.DELETE_WHILE_PROCESSING)
        db.execute(
            update(StepRun)
            .where(StepRun.pipeline_run_id == run.id, StepRun.status == "queued")
            .values(
                status="canceled",
                outcome="canceled",
                finished_at=now,
                failure_message="Canceled because the study was deleted",
            )
            .execution_options(synchronize_session=False)
        )
        current = "canceled"
    case.deleted_at = now
    record_event(
        db,
        "study_deleted",
        details={"status": current, "physical_delete": True},
        **_actor(request, current_user),
        case_id=case.id,
    )
    db.commit()
    LocalStudyStorage(settings.storage_root).remove_study(case.id)
    return StudyActionResponse(id=case.id, status=current, message="Study deleted")


def _read_last_lines(path: Path, lines: int) -> tuple[str, bool]:
    all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    truncated = len(all_lines) > lines
    return "\n".join(all_lines[-lines:]), truncated
