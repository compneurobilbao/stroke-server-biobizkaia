from __future__ import annotations

import shutil
import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import redis
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.models.processing_job import ProcessingJob
from app.models.notification import Notification
from app.models.share_link import ShareLink
from app.models.study import ClinicalReviewStatus, Study, StudyStatus
from app.models.user import User, UserRole
from app.schemas.admin import (
    AdminDashboardRead,
    AdminQueueSummary,
    AdminServiceHealth,
    AdminStorageSummary,
    AdminUserSummary,
)
from app.schemas.auth import (
    LoginRequest,
    LogoutResponse,
    NotificationPreferences,
    TokenResponse,
    UserActionResponse,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.schemas.notification import NotificationRead
from app.schemas.study import (
    ProcessingJobRead,
    ClinicalReviewUpdate,
    ShareLinkCreate,
    ShareLinkCreateResponse,
    ShareLinkRead,
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
    create_access_token,
    get_current_user,
    get_user_by_email,
    hash_password,
    normalize_email,
    require_admin,
    require_study_access,
    verify_password,
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

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/admin/dashboard", response_model=AdminDashboardRead)
def admin_dashboard(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AdminDashboardRead:
    studies_by_status = _count_by(db, Study.status, Study.deleted_at.is_(None))
    jobs_by_status = _count_by(db, ProcessingJob.status)
    worker_stats, worker_error = _inspect_worker_stats()
    worker_capacity = _worker_capacity(settings, worker_stats)
    queue = AdminQueueSummary(
        queued=jobs_by_status.get(StudyStatus.queued.value, 0),
        processing=jobs_by_status.get(StudyStatus.processing.value, 0),
        failed=jobs_by_status.get(StudyStatus.failed.value, 0),
        worker_replicas=worker_capacity["worker_replicas"],
        worker_concurrency=worker_capacity["worker_concurrency"],
        processing_capacity=worker_capacity["processing_capacity"],
    )
    queue.active = queue.queued + queue.processing
    queue.processing_available = max(0, queue.processing_capacity - queue.processing)
    users = _user_summary(db)
    studies_bytes = db.scalar(
        select(func.coalesce(func.sum(Study.file_size), 0)).where(
            Study.deleted_at.is_(None)
        )
    )
    storage = _storage_summary(settings.storage_root, int(studies_bytes or 0))
    services = _service_health(db, settings, worker_stats, worker_error)
    recent_failed_jobs = db.scalars(
        select(ProcessingJob)
        .where(ProcessingJob.status == StudyStatus.failed.value)
        .order_by(ProcessingJob.queued_at.desc())
        .limit(5)
    ).all()
    alerts = _dashboard_alerts(queue, storage, services)
    return AdminDashboardRead(
        generated_at=datetime.utcnow(),
        queue=queue,
        users=users,
        studies_by_status=studies_by_status,
        jobs_by_status=jobs_by_status,
        storage=storage,
        services=services,
        recent_failed_jobs=[
            ProcessingJobRead.model_validate(job) for job in recent_failed_jobs
        ],
        alerts=alerts,
    )


@router.post("/auth/login", response_model=TokenResponse)
def login(
    request: Request,
    payload: LoginRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> TokenResponse:
    user = get_user_by_email(db, payload.email)
    if (
        not user
        or not user.is_active
        or not verify_password(payload.password, user.hashed_password)
    ):
        record_event(
            db,
            "login_failed",
            details={"email": normalize_email(payload.email)},
            ip_address=client_ip(request),
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Credenciales no válidas"
        )

    user.last_login_at = datetime.utcnow()
    record_event(
        db,
        "login_succeeded",
        actor=user.email,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )
    db.commit()
    return TokenResponse(
        access_token=create_access_token(user, settings),
        user=_user_read(db, user),
    )


@router.post("/auth/logout", response_model=LogoutResponse)
def logout(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LogoutResponse:
    record_event(
        db,
        "logout",
        actor=current_user.email,
        actor_user_id=current_user.id,
        ip_address=client_ip(request),
    )
    db.commit()
    return LogoutResponse(message="Sesión cerrada")


@router.get("/auth/me", response_model=UserRead)
def current_user(
    current_user: User = Depends(get_current_user), db: Session = Depends(get_db)
) -> UserRead:
    return _user_read(db, current_user)


@router.get("/me/notification-preferences", response_model=NotificationPreferences)
def get_notification_preferences(
    current_user: User = Depends(get_current_user),
) -> NotificationPreferences:
    return NotificationPreferences(
        notify_on_processing_completed=current_user.notify_on_processing_completed,
        notify_on_processing_failed=current_user.notify_on_processing_failed,
    )


@router.patch("/me/notification-preferences", response_model=NotificationPreferences)
def update_notification_preferences(
    payload: NotificationPreferences,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationPreferences:
    current_user.notify_on_processing_completed = payload.notify_on_processing_completed
    current_user.notify_on_processing_failed = payload.notify_on_processing_failed
    record_event(
        db,
        "notification_preferences_updated",
        actor=current_user.email,
        actor_user_id=current_user.id,
        details=payload.model_dump(),
    )
    db.commit()
    return payload


@router.get("/notifications", response_model=list[NotificationRead])
def list_notifications(
    limit: int = Query(default=30, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[NotificationRead]:
    notifications = db.scalars(
        select(Notification)
        .where(Notification.recipient_user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    ).all()
    return [NotificationRead.model_validate(item) for item in notifications]


@router.post("/notifications/{notification_id}/read", response_model=NotificationRead)
def mark_notification_read(
    notification_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationRead:
    notification = db.get(Notification, notification_id)
    if not notification or notification.recipient_user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Notificación no encontrada"
        )
    if not notification.read_at:
        notification.read_at = datetime.utcnow()
        db.commit()
        db.refresh(notification)
    return NotificationRead.model_validate(notification)


@router.get("/users", response_model=list[UserRead])
def list_users(
    _: User = Depends(require_admin), db: Session = Depends(get_db)
) -> list[UserRead]:
    users = db.scalars(
        select(User).where(User.deleted_at.is_(None)).order_by(User.created_at.desc())
    ).all()
    return [_user_read(db, user) for user in users]


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(
    payload: UserCreate,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> UserRead:
    if payload.role not in {UserRole.admin.value, UserRole.researcher.value}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Rol no válido"
        )
    email = normalize_email(payload.email)
    _release_deleted_user_email(db, email)
    user = User(
        email=email,
        full_name=payload.full_name.strip(),
        hashed_password=hash_password(payload.password),
        role=payload.role,
        is_active=payload.is_active,
        storage_quota_bytes=payload.storage_quota_bytes,
    )
    db.add(user)
    try:
        record_event(
            db,
            "user_created",
            actor=current_user.email,
            actor_user_id=current_user.id,
            details={"email": user.email, "role": user.role},
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Ya existe un usuario con ese correo electrónico",
        ) from None
    db.refresh(user)
    return _user_read(db, user)


@router.patch("/users/{user_id}", response_model=UserRead)
def update_user(
    user_id: UUID,
    payload: UserUpdate,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> UserRead:
    user = db.get(User, user_id)
    if not user or user.deleted_at:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado"
        )
    if user.id == current_user.id and payload.is_active is False:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No puedes desactivar tu propio usuario administrador",
        )
    if "is_active" in payload.model_fields_set and payload.is_active is not None:
        if user.role == UserRole.admin.value and payload.is_active is False:
            _ensure_another_active_admin(db, user.id)
        user.is_active = payload.is_active
    if "storage_quota_bytes" in payload.model_fields_set:
        user.storage_quota_bytes = payload.storage_quota_bytes
    record_event(
        db,
        "user_updated",
        actor=current_user.email,
        actor_user_id=current_user.id,
        details={
            "user_id": str(user.id),
            "is_active": user.is_active,
            "storage_quota_bytes": user.storage_quota_bytes,
        },
    )
    db.commit()
    db.refresh(user)
    return _user_read(db, user)


@router.delete("/users/{user_id}", response_model=UserActionResponse)
def delete_user(
    user_id: UUID,
    current_user: User = Depends(require_admin),
    db: Session = Depends(get_db),
) -> UserActionResponse:
    user = db.get(User, user_id)
    if not user or user.deleted_at:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Usuario no encontrado"
        )
    if user.id == current_user.id:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No puedes borrar tu propio usuario administrador",
        )
    if user.role == UserRole.admin.value:
        _ensure_another_active_admin(db, user.id)
    original_email = user.email
    user.is_active = False
    user.deleted_at = datetime.utcnow()
    user.email = _deleted_user_email(user.email, user.id)
    record_event(
        db,
        "user_deleted",
        actor=current_user.email,
        actor_user_id=current_user.id,
        details={"user_id": str(user.id), "email": original_email},
    )
    db.commit()
    return UserActionResponse(id=user.id, message="Usuario borrado")


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
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="El fichero supera el tamaño máximo permitido",
        )
    if current_user.storage_quota_bytes is not None:
        used_bytes = _user_storage_used(db, current_user.id)
        if used_bytes + file_size > current_user.storage_quota_bytes:
            storage.remove_study(study_id)
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="La subida supera la cuota de almacenamiento del usuario",
            )

    bids_paths = None
    if processor_backend == "compneuro" and resolved_bids_subject_id:
        try:
            bids_paths = prepare_single_subject_t1w_bids(
                storage, study_id, stored_path, resolved_bids_subject_id
            )
        except Exception as exc:
            storage.remove_study(study_id)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="No se pudo preparar la estructura BIDS",
            ) from exc

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
        message="Estudio recibido y encolado para procesamiento",
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Estudio no encontrado"
        )
    if study.deleted_at:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Estudio no encontrado"
        )
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Estudio no encontrado"
        )
    if study.deleted_at:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Estudio no encontrado"
        )
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
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Estado de revisión clínica no válido",
        )
    if (
        payload.clinical_review_status != ClinicalReviewStatus.technical_only.value
        and study.status != StudyStatus.completed
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Solo se pueden revisar o validar estudios completados",
        )
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Estudio no encontrado"
        )
    require_study_access(study.owner_user_id, current_user)
    if study.deleted_at:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Estudio no encontrado"
        )
    if study.status != StudyStatus.completed or not study.pdf_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El informe PDF todavía no está disponible",
        )
    pdf_path = Path(study.pdf_path)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El informe PDF no existe en el almacenamiento",
        )
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
        filename=f"informe-tecnico-{study.id}.pdf",
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
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Estudio no encontrado"
        )
    require_study_access(study.owner_user_id, current_user)
    if study.deleted_at:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Estudio no encontrado"
        )
    if study.status != StudyStatus.completed or not study.output_zip_path:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El ZIP de resultados todavía no está disponible",
        )
    zip_path = Path(study.output_zip_path)
    if not zip_path.exists() or zip_path.suffix.lower() != ".zip":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El ZIP de resultados no existe en el almacenamiento",
        )
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


@router.post(
    "/studies/{study_id}/share-links",
    response_model=ShareLinkCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_share_link(
    request: Request,
    study_id: UUID,
    payload: ShareLinkCreate | None = None,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ShareLinkCreateResponse:
    study = _get_visible_study(db, study_id, current_user)
    _shareable_pdf_path(study)
    expires_in_hours = (
        payload.expires_in_hours
        if payload and payload.expires_in_hours
        else settings.share_link_expire_hours
    )
    expires_at = datetime.utcnow() + timedelta(hours=max(1, expires_in_hours))

    for _ in range(3):
        token = secrets.token_urlsafe(32)
        link = ShareLink(
            id=uuid4(),
            study_id=study.id,
            created_by_user_id=current_user.id,
            token_hash=_hash_share_token(token),
            expires_at=expires_at,
        )
        db.add(link)
        record_event(
            db,
            "share_link_created",
            study.id,
            {"share_link_id": str(link.id), "expires_at": expires_at.isoformat()},
            actor=current_user.email,
            actor_user_id=current_user.id,
            ip_address=client_ip(request),
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            continue
        db.refresh(link)
        return ShareLinkCreateResponse(
            **_share_link_read(link).model_dump(),
            url=str(request.url_for("download_shared_pdf", token=token)),
        )
    raise HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail="No se pudo crear el enlace de compartición",
    )


@router.get("/studies/{study_id}/share-links", response_model=list[ShareLinkRead])
def list_share_links(
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ShareLinkRead]:
    study = _get_visible_study(db, study_id, current_user)
    links = db.scalars(
        select(ShareLink)
        .where(ShareLink.study_id == study.id)
        .order_by(ShareLink.created_at.desc())
    ).all()
    return [_share_link_read(link) for link in links]


@router.post(
    "/studies/{study_id}/share-links/{link_id}/revoke",
    response_model=ShareLinkRead,
)
def revoke_share_link(
    request: Request,
    study_id: UUID,
    link_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ShareLinkRead:
    study = _get_visible_study(db, study_id, current_user)
    link = db.get(ShareLink, link_id)
    if not link or link.study_id != study.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Enlace no encontrado"
        )
    if not link.revoked_at:
        link.revoked_at = datetime.utcnow()
        record_event(
            db,
            "share_link_revoked",
            study.id,
            {"share_link_id": str(link.id)},
            actor=current_user.email,
            actor_user_id=current_user.id,
            ip_address=client_ip(request),
        )
        db.commit()
        db.refresh(link)
    return _share_link_read(link)


@router.get("/share/{token}/pdf")
def download_shared_pdf(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
) -> FileResponse:
    link = db.scalar(
        select(ShareLink).where(ShareLink.token_hash == _hash_share_token(token))
    )
    now = datetime.utcnow()
    if not link or link.revoked_at or link.expires_at <= now:
        _raise_public_share_not_found()
    study = link.study
    if (
        not study
        or study.deleted_at
        or study.status != StudyStatus.completed
        or not study.pdf_path
    ):
        _raise_public_share_not_found()
    pdf_path = Path(study.pdf_path)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        _raise_public_share_not_found()

    link.access_count = (link.access_count or 0) + 1
    link.last_accessed_at = now
    record_event(
        db,
        "share_link_pdf_downloaded",
        study.id,
        {"share_link_id": str(link.id), "access_count": link.access_count},
        actor="external-share",
        ip_address=client_ip(request),
    )
    db.commit()
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"informe-tecnico-{study.id}.pdf",
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Solo se pueden cancelar jobs en cola o en procesamiento",
        )
    job = _latest_job(study)
    was_processing = study.status == StudyStatus.processing
    if was_processing and (not job or not job.celery_task_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede cancelar un procesamiento sin tarea Celery asociada",
        )
    now = datetime.utcnow()
    if was_processing:
        updated_studies = db.execute(
            update(Study)
            .where(Study.id == study.id, Study.status == StudyStatus.processing)
            .values(error_message="Cancelación solicitada por el usuario")
        ).rowcount
        updated_jobs = db.execute(
            update(ProcessingJob)
            .where(
                ProcessingJob.id == job.id,
                ProcessingJob.status == StudyStatus.processing.value,
            )
            .values(error_message="Cancelación solicitada por el usuario")
        ).rowcount
        if updated_studies != 1 or updated_jobs != 1:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="El procesamiento ya no está activo",
            )
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
            message="Cancelación solicitada",
        )

    if job and job.celery_task_id:
        process_study.app.control.revoke(job.celery_task_id, terminate=False)

    study.status = StudyStatus.canceled
    study.processing_finished_at = now
    study.error_message = "Cancelado por el usuario"
    if job:
        job.status = StudyStatus.canceled.value
        job.finished_at = now
        job.error_message = "Cancelado antes de iniciar procesamiento"
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
        id=study.id, status=study.status.value, message="Procesamiento cancelado"
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Solo se pueden reintentar jobs fallidos",
        )
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
        id=study.id, status=study.status.value, message="Job reencolado"
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No se puede borrar un estudio en procesamiento",
        )
    job = _latest_job(study)
    if study.status == StudyStatus.queued:
        if job and job.celery_task_id:
            process_study.app.control.revoke(job.celery_task_id)
        study.status = StudyStatus.canceled
        if job:
            job.status = StudyStatus.canceled.value
            job.finished_at = datetime.utcnow()
            job.error_message = "Cancelado por borrado del estudio"
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
        id=study.id, status=study.status.value, message="Estudio borrado"
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


def _get_visible_study(db: Session, study_id: UUID, current_user: User) -> Study:
    study = db.get(Study, study_id)
    if not study or study.deleted_at:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Estudio no encontrado"
        )
    require_study_access(study.owner_user_id, current_user)
    return study


def _latest_job(study: Study) -> ProcessingJob | None:
    if not study.jobs:
        return None
    return max(study.jobs, key=lambda item: item.queued_at)


def _read_last_lines(path: Path, lines: int) -> tuple[str, bool]:
    all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    truncated = len(all_lines) > lines
    return "\n".join(all_lines[-lines:]), truncated


def _shareable_pdf_path(study: Study) -> Path:
    if study.status != StudyStatus.completed or not study.pdf_path:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Solo se pueden compartir estudios completados con PDF disponible",
        )
    pdf_path = Path(study.pdf_path)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="El informe PDF no existe en el almacenamiento",
        )
    return pdf_path


def _hash_share_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _share_link_read(link: ShareLink) -> ShareLinkRead:
    now = datetime.utcnow()
    return ShareLinkRead(
        id=link.id,
        study_id=link.study_id,
        created_at=link.created_at,
        expires_at=link.expires_at,
        revoked_at=link.revoked_at,
        last_accessed_at=link.last_accessed_at,
        access_count=link.access_count,
        is_expired=link.expires_at <= now,
        is_revoked=link.revoked_at is not None,
    )


def _raise_public_share_not_found() -> None:
    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND, detail="Informe no encontrado"
    )


def _count_by(db: Session, column, *conditions) -> dict[str, int]:
    query = select(column, func.count()).group_by(column)
    for condition in conditions:
        query = query.where(condition)
    return {_status_key(value): count for value, count in db.execute(query).all()}


def _status_key(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _user_summary(db: Session) -> AdminUserSummary:
    visible_users = User.deleted_at.is_(None)
    total = db.scalar(select(func.count()).select_from(User).where(visible_users)) or 0
    active = (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(visible_users, User.is_active.is_(True))
        )
        or 0
    )
    admins = (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(visible_users, User.role == UserRole.admin.value)
        )
        or 0
    )
    researchers = (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(visible_users, User.role == UserRole.researcher.value)
        )
        or 0
    )
    return AdminUserSummary(
        total=total, active=active, admins=admins, researchers=researchers
    )


def _ensure_another_active_admin(db: Session, user_id: UUID) -> None:
    active_admins = (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.id != user_id,
                User.role == UserRole.admin.value,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
        or 0
    )
    if active_admins < 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Debe quedar al menos un administrador activo",
        )


def _user_storage_used(db: Session, user_id: UUID) -> int:
    return int(
        db.scalar(
            select(func.coalesce(func.sum(Study.file_size), 0)).where(
                Study.owner_user_id == user_id,
                Study.deleted_at.is_(None),
            )
        )
        or 0
    )


def _user_read(db: Session, user: User) -> UserRead:
    return UserRead.model_validate(user).model_copy(
        update={"storage_used_bytes": _user_storage_used(db, user.id)}
    )


def _release_deleted_user_email(db: Session, email: str) -> None:
    deleted_user = db.scalar(
        select(User).where(User.email == email, User.deleted_at.is_not(None))
    )
    if deleted_user:
        deleted_user.email = _deleted_user_email(deleted_user.email, deleted_user.id)
        db.flush()


def _deleted_user_email(email: str, user_id: UUID) -> str:
    local, separator, domain = email.partition("@")
    if not separator:
        return f"deleted-{user_id}-{email}"
    return f"{local}+deleted-{user_id}@{domain}"


def _storage_summary(root: Path, studies_bytes: int) -> AdminStorageSummary:
    root = Path(root)
    exists = root.exists()
    disk_path = root if exists else root.parent
    while not disk_path.exists() and disk_path != disk_path.parent:
        disk_path = disk_path.parent
    try:
        usage = shutil.disk_usage(disk_path)
    except OSError:
        disk_total = disk_used = disk_free = 0
    else:
        disk_total = usage.total
        disk_used = usage.used
        disk_free = usage.free
    return AdminStorageSummary(
        root=str(root),
        exists=exists,
        studies_bytes=studies_bytes,
        disk_total_bytes=disk_total,
        disk_used_bytes=disk_used,
        disk_free_bytes=disk_free,
    )


def _service_health(
    db: Session,
    settings: Settings,
    worker_stats: dict[str, Any] | None,
    worker_error: str | None,
) -> list[AdminServiceHealth]:
    return [
        _database_health(db),
        _redis_health(settings),
        _worker_health(worker_stats, worker_error),
    ]


def _database_health(db: Session) -> AdminServiceHealth:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        return AdminServiceHealth(name="PostgreSQL", status="down", detail=str(exc))
    return AdminServiceHealth(name="PostgreSQL", status="ok")


def _redis_health(settings: Settings) -> AdminServiceHealth:
    broker_url = settings.celery_broker_url
    if not broker_url.startswith(("redis://", "rediss://")):
        return AdminServiceHealth(
            name="Redis", status="unknown", detail="Broker no Redis en este entorno"
        )
    try:
        client = redis.Redis.from_url(
            broker_url, socket_connect_timeout=0.5, socket_timeout=0.5
        )
        client.ping()
    except Exception as exc:
        return AdminServiceHealth(name="Redis", status="down", detail=str(exc))
    return AdminServiceHealth(name="Redis", status="ok")


def _inspect_worker_stats() -> tuple[dict[str, Any] | None, str | None]:
    try:
        from worker.celery_app import celery_app

        return celery_app.control.inspect(timeout=2.0).stats() or {}, None
    except Exception as exc:
        return None, str(exc)


def _worker_health(
    worker_stats: dict[str, Any] | None,
    worker_error: str | None,
) -> AdminServiceHealth:
    if worker_error:
        return AdminServiceHealth(name="Worker", status="down", detail=worker_error)
    if not worker_stats:
        return AdminServiceHealth(
            name="Worker", status="warning", detail="Sin respuesta de workers Celery"
        )
    return AdminServiceHealth(
        name="Worker", status="ok", detail=f"{len(worker_stats)} worker(s)"
    )


def _worker_capacity(
    settings: Settings,
    worker_stats: dict[str, Any] | None,
) -> dict[str, int]:
    fallback_concurrency = max(1, settings.max_concurrent_processing_jobs)
    if not worker_stats:
        return {
            "worker_replicas": 0,
            "worker_concurrency": fallback_concurrency,
            "processing_capacity": 0,
        }

    worker_replicas = len(worker_stats)
    capacities = []
    for stats in worker_stats.values():
        pool = stats.get("pool") or {}
        raw_capacity = pool.get("max-concurrency") or pool.get("max_concurrency")
        try:
            capacities.append(max(1, int(raw_capacity)))
        except (TypeError, ValueError):
            capacities.append(fallback_concurrency)

    return {
        "worker_replicas": worker_replicas,
        "worker_concurrency": max(capacities) if capacities else fallback_concurrency,
        "processing_capacity": sum(capacities),
    }


def _dashboard_alerts(
    queue: AdminQueueSummary,
    storage: AdminStorageSummary,
    services: list[AdminServiceHealth],
) -> list[str]:
    alerts = []
    if queue.failed:
        alerts.append(f"Hay {queue.failed} job(s) fallidos que requieren revisión")
    if queue.queued:
        alerts.append(f"Hay {queue.queued} job(s) esperando en cola")
    if (
        storage.disk_total_bytes
        and storage.disk_free_bytes / storage.disk_total_bytes < 0.1
    ):
        alerts.append("Queda menos del 10% de disco libre")
    for service in services:
        if service.status in {"down", "warning"}:
            alerts.append(f"{service.name}: {service.detail or service.status}")
    return alerts
