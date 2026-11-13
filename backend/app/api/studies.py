from __future__ import annotations

from datetime import datetime
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
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, api_error
from app.db.session import get_db
from app.models.processing_job import ProcessingJob
from app.models.study import ClinicalReviewStatus, Study, StudyStatus
from app.models.user import User, UserRole
from app.schemas.study import (
    ProcessingJobRead,
    ClinicalReviewUpdate,
    StudyActionResponse,
    StudyDetailRead,
    StudyLogEntry,
    StudyLogsRead,
    StudyRead,
    StudyStatusRead,
    UploadResponse,
)
from app.services.audit import record_event
from app.services.auth import (
    client_ip,
    get_current_user,
    require_study_access,
)
from app.services.bids import prepare_single_subject_t1w_bids
from app.services.security import (
    resolve_bids_subject_id,
    sanitize_filename,
    validate_nifti_gz_filename,
    validate_upload,
)
from app.services.storage import LocalStudyStorage
from worker.tasks import process_study
from app.api.helpers import _get_visible_study, _latest_job, _user_storage_used

router = APIRouter()


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
    processor_backend = settings.processor_backend.strip().lower()
    if processor_backend == "compneuro":
        safe_filename = sanitize_filename(file.filename or "")
        validate_nifti_gz_filename(safe_filename)
    else:
        safe_filename = validate_upload(file, settings.allowed_extension_list)
    study_id = uuid4()
    resolved_bids_subject_id = (
        resolve_bids_subject_id(bids_subject_id, study_id)
        if processor_backend == "compneuro" or bids_subject_id
        else None
    )
    storage = LocalStudyStorage(settings.storage_root)

    stored_path, file_size, checksum = storage.save_upload(
        study_id, file, safe_filename
    )
    if file_size > settings.max_upload_size_bytes:
        storage.remove_study(study_id)
        raise api_error(ErrorCode.UPLOAD_TOO_LARGE)
    if current_user.storage_quota_bytes is not None:
        used_bytes = _user_storage_used(db, current_user.id)
        if used_bytes + file_size > current_user.storage_quota_bytes:
            storage.remove_study(study_id)
            raise api_error(ErrorCode.STORAGE_QUOTA_EXCEEDED)

    bids_paths = None
    if processor_backend == "compneuro" and resolved_bids_subject_id:
        try:
            bids_paths = prepare_single_subject_t1w_bids(
                storage, study_id, stored_path, resolved_bids_subject_id
            )
        except Exception as exc:
            storage.remove_study(study_id)
            raise api_error(ErrorCode.BIDS_PREPARATION_FAILED) from exc

    study = Study(
        id=study_id,
        owner_user_id=current_user.id,
        original_filename=file.filename or safe_filename,
        stored_path=str(stored_path),
        output_path=str(storage.output_dir(study_id)),
        bids_path=str(bids_paths.bids_project_dir) if bids_paths else None,
        preproc_output_path=str(storage.preproc_output_dir(study_id))
        if processor_backend == "compneuro"
        else None,
        bids_subject_id=resolved_bids_subject_id,
        processor_backend=processor_backend,
        container_image=settings.compneuro_container_image
        if processor_backend == "compneuro"
        else None,
        status=StudyStatus.queued,
        processor_name=settings.processor_name,
        processor_version=settings.processor_version,
        file_size=file_size,
        checksum=checksum,
    )
    job = ProcessingJob(study_id=study_id, status=StudyStatus.queued.value)
    db.add(study)
    db.add(job)
    record_event(
        db,
        "study_uploaded",
        study_id,
        {
            "filename": safe_filename,
            "processor_backend": processor_backend,
            "bids_subject_id": resolved_bids_subject_id,
        },
        actor=current_user.email,
        actor_user_id=current_user.id,
        ip_address=request.client.host if request.client else None,
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

    async_result = process_study.delay(str(study_id), str(job.id))
    if getattr(async_result, "id", None):
        job.celery_task_id = async_result.id
        db.commit()
    return UploadResponse(
        id=study_id,
        status=study.status.value,
        message="Study received and queued for processing",
        bids_subject_id=resolved_bids_subject_id,
    )


@router.get("/studies", response_model=list[StudyRead])
def list_studies(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> list[StudyRead]:
    query = (
        select(Study)
        .where(Study.deleted_at.is_(None))
        .order_by(Study.created_at.desc())
    )
    if current_user.role != UserRole.admin.value:
        query = query.where(Study.owner_user_id == current_user.id)
    studies = db.scalars(query).all()
    return [to_study_read(study) for study in studies]


@router.get("/studies/{study_id}", response_model=StudyRead)
def get_study(
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudyRead:
    study = db.get(Study, study_id)
    if not study:
        raise api_error(ErrorCode.STUDY_NOT_FOUND)
    if study.deleted_at:
        raise api_error(ErrorCode.STUDY_NOT_FOUND)
    require_study_access(study.owner_user_id, current_user)
    return to_study_read(study)


@router.get("/studies/{study_id}/detail", response_model=StudyDetailRead)
def get_study_detail(
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudyDetailRead:
    study = _get_visible_study(db, study_id, current_user)
    jobs = sorted(study.jobs, key=lambda item: item.queued_at, reverse=True)
    return StudyDetailRead(
        **to_study_read(study).model_dump(),
        jobs=[ProcessingJobRead.model_validate(job) for job in jobs],
    )


@router.get("/studies/{study_id}/status", response_model=StudyStatusRead)
def get_study_status(
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudyStatusRead:
    study = db.get(Study, study_id)
    if not study:
        raise api_error(ErrorCode.STUDY_NOT_FOUND)
    if study.deleted_at:
        raise api_error(ErrorCode.STUDY_NOT_FOUND)
    require_study_access(study.owner_user_id, current_user)
    return StudyStatusRead(
        id=study.id,
        status=study.status.value,
        error_message=study.error_message,
        has_pdf=bool(study.pdf_path),
        has_output_zip=bool(study.output_zip_path),
        processing_warnings=study.processing_warnings,
        clinical_review_status=study.clinical_review_status,
        updated_at=study.updated_at,
    )


@router.patch("/studies/{study_id}/clinical-review", response_model=StudyRead)
def update_clinical_review_status(
    request: Request,
    study_id: UUID,
    payload: ClinicalReviewUpdate,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudyRead:
    study = _get_visible_study(db, study_id, current_user)
    allowed_statuses = {item.value for item in ClinicalReviewStatus}
    if payload.clinical_review_status not in allowed_statuses:
        raise api_error(ErrorCode.INVALID_CLINICAL_REVIEW_STATUS)
    if (
        payload.clinical_review_status != ClinicalReviewStatus.technical_only.value
        and study.status != StudyStatus.completed
    ):
        raise api_error(ErrorCode.CLINICAL_REVIEW_REQUIRES_COMPLETED)
    study.clinical_review_status = payload.clinical_review_status
    record_event(
        db,
        "study_clinical_review_updated",
        study.id,
        {"clinical_review_status": study.clinical_review_status},
        actor=current_user.email,
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(study)
    return to_study_read(study)


@router.get("/studies/{study_id}/download")
def download_pdf(
    request: Request,
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    study = db.get(Study, study_id)
    if not study:
        raise api_error(ErrorCode.STUDY_NOT_FOUND)
    require_study_access(study.owner_user_id, current_user)
    if study.deleted_at:
        raise api_error(ErrorCode.STUDY_NOT_FOUND)
    if study.status != StudyStatus.completed or not study.pdf_path:
        raise api_error(ErrorCode.PDF_NOT_READY)
    pdf_path = Path(study.pdf_path)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise api_error(ErrorCode.PDF_MISSING_IN_STORAGE)
    record_event(
        db,
        "study_downloaded_pdf",
        study.id,
        actor=current_user.email,
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    db.commit()
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"technical-report-{study.id}.pdf",
    )


@router.get("/studies/{study_id}/download/pdf")
def download_technical_pdf(
    request: Request,
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    return download_pdf(request, study_id, current_user, db)


@router.get("/studies/{study_id}/download/zip")
def download_outputs_zip(
    request: Request,
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> FileResponse:
    study = db.get(Study, study_id)
    if not study:
        raise api_error(ErrorCode.STUDY_NOT_FOUND)
    require_study_access(study.owner_user_id, current_user)
    if study.deleted_at:
        raise api_error(ErrorCode.STUDY_NOT_FOUND)
    if study.status != StudyStatus.completed or not study.output_zip_path:
        raise api_error(ErrorCode.ZIP_NOT_READY)
    zip_path = Path(study.output_zip_path)
    if not zip_path.exists() or zip_path.suffix.lower() != ".zip":
        raise api_error(ErrorCode.ZIP_MISSING_IN_STORAGE)
    record_event(
        db,
        "study_downloaded_zip",
        study.id,
        actor=current_user.email,
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    db.commit()
    return FileResponse(
        zip_path, media_type="application/zip", filename=f"outputs-{study.id}.zip"
    )


@router.get("/studies/{study_id}/logs", response_model=StudyLogsRead)
def get_study_logs(
    study_id: UUID,
    lines: int = Query(default=200, ge=1, le=1000),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StudyLogsRead:
    study = _get_visible_study(db, study_id, current_user)
    storage = LocalStudyStorage(settings.storage_root)
    logs = []
    for name in ["processor.log", "rendering.log"]:
        path = storage.logs_dir(study.id) / name
        if path.exists() and path.is_file():
            content, truncated = _read_last_lines(path, lines)
            logs.append(StudyLogEntry(name=name, content=content, truncated=truncated))
    return StudyLogsRead(study_id=study.id, logs=logs)


@router.post("/studies/{study_id}/cancel", response_model=StudyActionResponse)
def cancel_study_processing(
    request: Request,
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudyActionResponse:
    study = _get_visible_study(db, study_id, current_user)
    if study.status not in {StudyStatus.queued, StudyStatus.processing}:
        raise api_error(ErrorCode.CANCEL_INVALID_STATE)
    job = _latest_job(study)
    was_processing = study.status == StudyStatus.processing
    if was_processing and (not job or not job.celery_task_id):
        raise api_error(ErrorCode.CANCEL_WITHOUT_TASK)
    now = datetime.utcnow()
    if was_processing:
        updated_studies = db.execute(
            update(Study)
            .where(Study.id == study.id, Study.status == StudyStatus.processing)
            .values(error_message="Cancellation requested by the user")
        ).rowcount
        updated_jobs = db.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.id == job.id,
                ProcessingJob.status == StudyStatus.processing.value,
            )
            .values(
                error_message="Cancellation requested by the user",
                cancel_requested_at=now,
            )
        ).rowcount
        if updated_studies != 1 or updated_jobs != 1:
            db.rollback()
            raise api_error(ErrorCode.PROCESSING_NOT_ACTIVE)
        record_event(
            db,
            "study_cancellation_requested",
            study.id,
            {"job_id": str(job.id) if job else None},
            actor=current_user.email,
            actor_user_id=current_user.id,
            ip_address=client_ip(request),
        )
        db.commit()
        db.refresh(study)
        process_study.app.control.revoke(
            job.celery_task_id,
            terminate=True,
            signal="SIGTERM",
        )
        return StudyActionResponse(
            id=study.id,
            status=study.status.value,
            message="Cancellation requested",
        )

    if job and job.celery_task_id:
        process_study.app.control.revoke(job.celery_task_id, terminate=False)

    study.status = StudyStatus.canceled
    study.processing_finished_at = now
    study.error_message = "Canceled by the user"
    if job:
        job.status = StudyStatus.canceled.value
        job.finished_at = now
        job.error_message = "Canceled before processing started"
    record_event(
        db,
        "study_canceled",
        study.id,
        {"job_id": str(job.id) if job else None},
        actor=current_user.email,
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    db.commit()
    return StudyActionResponse(
        id=study.id, status=study.status.value, message="Processing canceled"
    )


@router.post("/studies/{study_id}/retry", response_model=StudyActionResponse)
def retry_failed_study(
    request: Request,
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StudyActionResponse:
    study = _get_visible_study(db, study_id, current_user)
    if study.status != StudyStatus.failed:
        raise api_error(ErrorCode.RETRY_INVALID_STATE)
    retry_count = max((job.retry_count for job in study.jobs), default=0) + 1
    job = ProcessingJob(
        study_id=study.id, status=StudyStatus.queued.value, retry_count=retry_count
    )
    study.status = StudyStatus.queued
    study.error_message = None
    study.processing_started_at = None
    study.processing_finished_at = None
    study.pdf_path = None
    study.output_zip_path = None
    study.processing_warnings = None
    db.add(job)
    record_event(
        db,
        "study_retry_queued",
        study.id,
        {"retry_count": retry_count},
        actor=current_user.email,
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    db.commit()
    async_result = process_study.delay(str(study.id), str(job.id))
    if getattr(async_result, "id", None):
        job.celery_task_id = async_result.id
        db.commit()
    return StudyActionResponse(
        id=study.id, status=study.status.value, message="Job requeued"
    )


@router.delete("/studies/{study_id}", response_model=StudyActionResponse)
def delete_study(
    request: Request,
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> StudyActionResponse:
    study = _get_visible_study(db, study_id, current_user)
    if study.status == StudyStatus.processing:
        raise api_error(ErrorCode.DELETE_WHILE_PROCESSING)
    job = _latest_job(study)
    if study.status == StudyStatus.queued:
        if job and job.celery_task_id:
            process_study.app.control.revoke(job.celery_task_id)
        study.status = StudyStatus.canceled
        if job:
            job.status = StudyStatus.canceled.value
            job.finished_at = datetime.utcnow()
            job.error_message = "Canceled because the study was deleted"
    study.deleted_at = datetime.utcnow()
    record_event(
        db,
        "study_deleted",
        study.id,
        {"status": study.status.value, "physical_delete": True},
        actor=current_user.email,
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    db.commit()
    LocalStudyStorage(settings.storage_root).remove_study(study.id)
    return StudyActionResponse(
        id=study.id, status=study.status.value, message="Study deleted"
    )


def to_study_read(study: Study) -> StudyRead:
    return StudyRead(
        id=study.id,
        owner_user_id=study.owner_user_id,
        original_filename=study.original_filename,
        status=study.status.value,
        created_at=study.created_at,
        updated_at=study.updated_at,
        processing_started_at=study.processing_started_at,
        processing_finished_at=study.processing_finished_at,
        deleted_at=study.deleted_at,
        keep_forever=study.keep_forever,
        error_message=study.error_message,
        processor_name=study.processor_name,
        processor_version=study.processor_version,
        processor_backend=study.processor_backend,
        bids_subject_id=study.bids_subject_id,
        file_size=study.file_size,
        checksum=study.checksum,
        has_pdf=bool(study.pdf_path),
        has_output_zip=bool(study.output_zip_path),
        processing_warnings=study.processing_warnings,
        clinical_review_status=study.clinical_review_status,
    )


def _read_last_lines(path: Path, lines: int) -> tuple[str, bool]:
    all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    truncated = len(all_lines) > lines
    return "\n".join(all_lines[-lines:]), truncated
