"""Storage quota of a user (docs/data-model.md section 12.4, owner decision 4).

Usage = legacy study uploads (as counted by the legacy endpoints) + every input file and
every stored artefact of the user's live cases created through the v1 API. Back-filled
legacy cases are skipped, their bytes are already counted through ``studies``.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.v1.errors import v1_error
from app.models.artifact import Artifact
from app.models.case import Case, DeploymentProfile, InputFile
from app.models.study import Study
from app.models.user import User


def _live_cases(user_id: UUID):
    return (
        select(Case.id)
        .where(
            Case.owner_user_id == user_id,
            Case.deleted_at.is_(None),
            Case.deployment_profile != DeploymentProfile.legacy.value,
        )
        .scalar_subquery()
    )


def storage_used(db: Session, user_id: UUID) -> int:
    studies = db.scalar(
        select(func.coalesce(func.sum(Study.file_size), 0)).where(
            Study.owner_user_id == user_id, Study.deleted_at.is_(None)
        )
    )
    inputs = db.scalar(
        select(func.coalesce(func.sum(InputFile.size_bytes), 0)).where(
            InputFile.case_id.in_(_live_cases(user_id))
        )
    )
    artefacts = db.scalar(
        select(func.coalesce(func.sum(Artifact.size_bytes), 0)).where(
            Artifact.case_id.in_(_live_cases(user_id))
        )
    )
    return int(studies or 0) + int(inputs or 0) + int(artefacts or 0)


def check_quota(
    db: Session, owner: User, additional_bytes: int, replaced_bytes: int = 0
) -> None:
    """Refuse (413) when ``additional_bytes`` would exceed the owner's quota."""
    if owner.storage_quota_bytes is None:
        return
    used = storage_used(db, owner.id) - replaced_bytes
    if used + additional_bytes > owner.storage_quota_bytes:
        raise v1_error(
            413,
            "storage_quota_exceeded",
            "The operation exceeds the storage quota of the case owner",
        )
