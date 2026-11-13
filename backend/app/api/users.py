from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    status,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, api_error
from app.db.session import get_db
from app.models.user import User, UserRole
from app.schemas.auth import (
    UserActionResponse,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.services.audit import record_event
from app.services.auth import (
    hash_password,
    normalize_email,
    require_admin,
)
from app.api.helpers import _user_read

router = APIRouter()


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
        raise api_error(ErrorCode.INVALID_ROLE)
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
        raise api_error(ErrorCode.USER_EMAIL_EXISTS) from None
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
        raise api_error(ErrorCode.USER_NOT_FOUND)
    if user.id == current_user.id and payload.is_active is False:
        raise api_error(ErrorCode.CANNOT_DEACTIVATE_SELF)
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
        raise api_error(ErrorCode.USER_NOT_FOUND)
    if user.id == current_user.id:
        raise api_error(ErrorCode.CANNOT_DELETE_SELF)
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
    return UserActionResponse(id=user.id, message="User deleted")


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
        raise api_error(ErrorCode.LAST_ADMIN_REQUIRED)


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
