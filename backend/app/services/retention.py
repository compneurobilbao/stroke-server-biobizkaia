"""Retention policy over cases (docs/data-model.md section 6, "retention" row).

Since the legacy cut-over (migration 0012) candidates are non-deleted cases with
``keep_forever = false``, created before the cut-off, with no active run. Deletion is a
soft delete of the case plus removal of its files, as for the legacy studies before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.case import Case
from app.models.pipeline_run import ACTIVE_STATUSES, PipelineRun
from app.services import legacy_studies
from app.services.audit import record_event
from app.services.storage import LocalStudyStorage


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

    now = legacy_studies.naive_utc(now) if now else datetime.utcnow()
    cutoff = now - timedelta(days=retention_days)
    active_cases = select(PipelineRun.case_id).where(
        PipelineRun.status.in_(ACTIVE_STATUSES)
    )
    cases = list(
        db.scalars(
            select(Case)
            .where(
                Case.deleted_at.is_(None),
                Case.keep_forever.is_(False),
                Case.created_at <= cutoff.replace(tzinfo=timezone.utc),
                Case.id.not_in(active_cases),
            )
            .order_by(Case.created_at.asc())
        )
    )
    reads = legacy_studies.study_reads(db, cases)
    candidates = [
        RetentionCandidate(
            id=read.id,
            original_filename=read.original_filename,
            status=read.status,
            created_at=read.created_at,
        )
        for read in reads
    ]

    if dry_run:
        return RetentionResult(
            dry_run=True,
            retention_days=retention_days,
            cutoff=cutoff,
            candidates=candidates,
            deleted=[],
        )

    deleted_at = now.replace(tzinfo=timezone.utc)
    for case, candidate in zip(cases, candidates):
        case.deleted_at = deleted_at
        record_event(
            db,
            "study_retention_deleted",
            details={
                "status": candidate.status,
                "retention_days": retention_days,
                "cutoff": cutoff.isoformat(),
                "physical_delete": True,
            },
            actor="system/retention",
            case_id=case.id,
        )
    db.commit()

    for case in cases:
        storage.remove_study(case.id)

    return RetentionResult(
        dry_run=False,
        retention_days=retention_days,
        cutoff=cutoff,
        candidates=candidates,
        deleted=candidates,
    )
