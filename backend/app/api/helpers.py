from __future__ import annotations

from uuid import UUID

from sqlalchemy.orm import Session

from app.models.user import User
from app.schemas.auth import (
    UserRead,
)
from app.services.quota import storage_used


def _user_storage_used(db: Session, user_id: UUID) -> int:
    """Same figure as the v1 quota (inputs plus artefacts of the user's live cases)."""
    return storage_used(db, user_id)


def _user_read(db: Session, user: User) -> UserRead:
    return UserRead.model_validate(user).model_copy(
        update={"storage_used_bytes": _user_storage_used(db, user.id)}
    )
