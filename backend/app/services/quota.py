"""Storage usage (docs/data-model.md section 6 "quota" and section 12, owner decision 4).

Usage of a user = ``size_bytes`` of every input file plus every stored artefact of the
user's non-deleted cases, whatever API created them. Since the legacy cut-over
(migration 0012) a legacy upload is a case with one ``legacy_upload`` input file, so the
legacy endpoints, the users endpoint and /api/v1 all count the same bytes. Legacy
artefacts back-filled from ``studies`` have no recorded size and count as 0 until a
later command fills it.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.artifact import Artifact
from app.models.case import Case, InputFile


def _live_cases(user_id: UUID | None):
    query = select(Case.id).where(Case.deleted_at.is_(None))
    if user_id is not None:
        query = query.where(Case.owner_user_id == user_id)
    return query.scalar_subquery()


def storage_used(db: Session, user_id: UUID | None) -> int:
    """Bytes used by ``user_id`` (all users when None)."""
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
    return int(inputs or 0) + int(artefacts or 0)
