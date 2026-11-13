from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta
from pathlib import Path
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
from app.models.share_link import ShareLink
from app.models.study import Study, StudyStatus
from app.models.user import User
from app.schemas.study import (
    ShareLinkCreate,
    ShareLinkCreateResponse,
    ShareLinkRead,
)
from app.services.audit import record_event
from app.services.auth import (
    client_ip,
    get_current_user,
)
from app.api.helpers import _get_visible_study

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
    raise api_error(ErrorCode.SHARE_LINK_CREATION_FAILED)


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
        raise api_error(ErrorCode.SHARE_LINK_NOT_FOUND)
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
        filename=f"technical-report-{study.id}.pdf",
    )


def _shareable_pdf_path(study: Study) -> Path:
    if study.status != StudyStatus.completed or not study.pdf_path:
        raise api_error(ErrorCode.SHARE_REQUIRES_COMPLETED_PDF)
    pdf_path = Path(study.pdf_path)
    if not pdf_path.exists() or pdf_path.suffix.lower() != ".pdf":
        raise api_error(ErrorCode.PDF_MISSING_IN_STORAGE)
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
    raise api_error(ErrorCode.REPORT_NOT_FOUND)
