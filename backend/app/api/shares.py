from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from fastapi import (
    APIRouter,
    Depends,
    Request,
    status,
)
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.core.errors import ErrorCode, api_error
from app.db.session import get_db
from app.models.artifact import Report
from app.models.case import Case
from app.models.share_link import ShareLink
from app.models.user import User
from app.schemas.study import (
    ShareLinkCreate,
    ShareLinkCreateResponse,
    ShareLinkRead,
)
from app.services import legacy_studies as legacy
from app.services.audit import record_event
from app.services.auth import (
    client_ip,
    get_current_user,
)
from app.services.storage import LocalStudyStorage

router = APIRouter()


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
    """A link bound to the case and to its current report (docs/data-model.md 10.4)."""
    case = legacy.visible_case(db, study_id, current_user)
    report = _shareable_report(db, case, LocalStudyStorage(settings.storage_root))
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
            # Back-filled cases keep their study id so that a downgrade keeps the link.
            study_id=case.legacy_study_id,
            case_id=case.id,
            report_id=report.id,
            created_by_user_id=current_user.id,
            token_hash=_hash_share_token(token),
            expires_at=expires_at,
        )
        db.add(link)
        record_event(
            db,
            "share_link_created",
            details={
                "share_link_id": str(link.id),
                "expires_at": expires_at.isoformat(),
            },
            actor=current_user.email,
            actor_user_id=current_user.id,
            ip_address=client_ip(request),
            case_id=case.id,
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
    raise api_error(ErrorCode.SHARE_LINK_CREATION_FAILED)


@router.get("/studies/{study_id}/share-links", response_model=list[ShareLinkRead])
def list_share_links(
    study_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[ShareLinkRead]:
    case = legacy.visible_case(db, study_id, current_user)
    links = db.scalars(
        select(ShareLink)
        .where(ShareLink.case_id == case.id)
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
    case = legacy.visible_case(db, study_id, current_user)
    link = db.get(ShareLink, link_id)
    if not link or link.case_id != case.id:
        raise api_error(ErrorCode.SHARE_LINK_NOT_FOUND)
    if not link.revoked_at:
        link.revoked_at = datetime.utcnow()
        record_event(
            db,
            "share_link_revoked",
            details={"share_link_id": str(link.id)},
            actor=current_user.email,
            actor_user_id=current_user.id,
            ip_address=client_ip(request),
            case_id=case.id,
        )
        db.commit()
        db.refresh(link)
    return _share_link_read(link)


@router.get("/share/{token}/pdf")
def download_shared_pdf(
    request: Request,
    token: str,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """The PDF of the bound report or, for links without one (created before the
    cut-over), of the case's current report (docs/data-model.md section 9)."""
    link = db.scalar(
        select(ShareLink).where(ShareLink.token_hash == _hash_share_token(token))
    )
    now = datetime.utcnow()
    if not link or link.revoked_at or link.expires_at <= now:
        _raise_public_share_not_found()
    case = db.get(Case, link.case_id)
    if not case or case.deleted_at or case.deployment_profile != legacy.LEGACY_PROFILE:
        _raise_public_share_not_found()
    if link.report_id is not None:
        report = db.get(Report, link.report_id)
    else:
        run = legacy.latest_run(db, case.id)
        report = (
            legacy.current_report(db, run)
            if legacy.study_status(run) == "completed"
            else None
        )
    pdf_path = legacy.report_pdf_path(
        db, LocalStudyStorage(settings.storage_root), report
    )
    if pdf_path is None or not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        _raise_public_share_not_found()

    link.access_count = (link.access_count or 0) + 1
    link.last_accessed_at = now
    record_event(
        db,
        "share_link_pdf_downloaded",
        details={"share_link_id": str(link.id), "access_count": link.access_count},
        actor="external-share",
        ip_address=client_ip(request),
        case_id=case.id,
    )
    db.commit()
    return FileResponse(
        pdf_path,
        media_type="application/pdf",
        filename=f"technical-report-{case.id}.pdf",
    )


def _shareable_report(db: Session, case: Case, storage: LocalStudyStorage) -> Report:
    run = legacy.latest_run(db, case.id)
    report = legacy.current_report(db, run)
    if legacy.study_status(run) != "completed" or report is None:
        raise api_error(ErrorCode.SHARE_REQUIRES_COMPLETED_PDF)
    pdf_path = legacy.report_pdf_path(db, storage, report)
    if pdf_path is None or not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        raise api_error(ErrorCode.PDF_MISSING_IN_STORAGE)
    return report


def _hash_share_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _share_link_read(link: ShareLink) -> ShareLinkRead:
    now = datetime.utcnow()
    return ShareLinkRead(
        id=link.id,
        study_id=link.case_id,
        created_at=link.created_at,
        expires_at=link.expires_at,
        revoked_at=link.revoked_at,
        last_accessed_at=link.last_accessed_at,
        access_count=link.access_count,
        is_expired=link.expires_at <= now,
        is_revoked=link.revoked_at is not None,
    )


def _raise_public_share_not_found() -> None:
    raise api_error(ErrorCode.REPORT_NOT_FOUND)
