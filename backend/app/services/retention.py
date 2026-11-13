from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.study import Study, StudyStatus
from app.services.audit import record_event
from app.services.storage import LocalStudyStorage

TERMINAL_RETENTION_STATUSES = (
    StudyStatus.completed,
    StudyStatus.failed,
    StudyStatus.canceled,
)


@dataclass(frozen=True)
class RetentionCandidate:
    id: UUID
    original_filename: str
    status: str
    created_at: datetime


@dataclass(frozen=True)
class RetentionResult:
    dry_run: bool
    retention_days: int
    cutoff: datetime
    candidates: list[RetentionCandidate]
    deleted: list[RetentionCandidate]


def apply_retention_policy(
    db: Session,
    storage: LocalStudyStorage,
    retention_days: int,
    *,
    dry_run: bool = True,
    now: datetime | None = None,
) -> RetentionResult:
    if retention_days < 1:
        raise ValueError("retention_days must be greater than or equal to 1")

    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=retention_days)
    studies = list(
        db.scalars(
            select(Study)
            .where(
                Study.deleted_at.is_(None),
                Study.keep_forever.is_(False),
                Study.status.in_(TERMINAL_RETENTION_STATUSES),
                Study.created_at <= cutoff,
            )
            .order_by(Study.created_at.asc())
        )
    )
    candidates = [_to_candidate(study) for study in studies]

    if dry_run:
        return RetentionResult(
            dry_run=True,
            retention_days=retention_days,
            cutoff=cutoff,
            candidates=candidates,
            deleted=[],
        )

    deleted_at = now
    for study in studies:
        study.deleted_at = deleted_at
        record_event(
            db,
            "study_retention_deleted",
            study.id,
            {
                "status": study.status.value,
                "retention_days": retention_days,
                "cutoff": cutoff.isoformat(),
                "physical_delete": True,
            },
            actor="system/retention",
        )
    db.commit()

    for study in studies:
        storage.remove_study(study.id)

    return RetentionResult(
        dry_run=False,
        retention_days=retention_days,
        cutoff=cutoff,
        candidates=candidates,
        deleted=candidates,
    )


def _to_candidate(study: Study) -> RetentionCandidate:
    return RetentionCandidate(
        id=study.id,
        original_filename=study.original_filename,
        status=study.status.value,
        created_at=study.created_at,
    )
