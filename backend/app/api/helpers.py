from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, api_error
from app.models.processing_job import ProcessingJob
from app.models.study import Study
from app.models.user import User
from app.schemas.auth import (
    UserRead,
)
from app.services.auth import (
    require_study_access,
)


def _get_visible_study(db: Session, study_id: UUID, current_user: User) -> Study:
    study = db.get(Study, study_id)
    if not study or study.deleted_at:
        raise api_error(ErrorCode.STUDY_NOT_FOUND)
    require_study_access(study.owner_user_id, current_user)
    return study


def _latest_job(study: Study) -> ProcessingJob | None:
    if not study.jobs:
        return None
    return max(study.jobs, key=lambda item: item.queued_at)


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
