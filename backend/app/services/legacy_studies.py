"""Legacy study endpoints as aliases over the case model (docs/data-model.md 10.4, stage 2).

From migration 0012 on, ``studies`` and ``processing_jobs`` are frozen: nothing writes
them. A legacy "study" is a case (legacy study id = case id); its status, jobs, report
and ZIP are derived from the case's latest run, its step attempts and its artefacts.
This module holds the read side (building the unchanged legacy response models) and
the identifiers and mappings shared by the legacy endpoints and the legacy worker task.

Status mapping (run -> study): ``queued`` -> ``queued``; ``running`` and
``awaiting_review`` -> ``processing``; ``succeeded`` -> ``completed``; ``failed``,
``canceled`` unchanged. A case without any run is ``uploaded``. Step attempts map to
legacy jobs with the same table.

Legacy timestamps were naive UTC; the new tables hold ``timestamptz``. Responses keep
the legacy format (naive UTC) so that the payloads are byte-for-byte the same shape.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, api_error
from app.models.artifact import Artifact, Report
from app.models.case import LEGACY_UPLOAD, Case, InputFile
from app.models.formats import KEBAB
from app.models.pipeline_run import PipelineRun, StepRun
from app.models.user import User, UserRole
from app.schemas.study import ProcessingJobRead, StudyRead, StudyStatusRead
from app.services.auth import require_study_access
from app.services.storage import LocalStudyStorage

# Derived identifiers of docs/data-model.md 10.2 (same namespace as migration 0011).
LEGACY_NS = uuid.uuid5(uuid.NAMESPACE_URL, "urn:stroke-server:legacy")

LEGACY_ZIP_TYPE = "legacy_outputs_zip"
LEGACY_PDF_TYPE = "legacy_technical_report_pdf"
NO_RUN_STATUS = "uploaded"
STUDY_STATUS = {
    "queued": "queued",
    "running": "processing",
    "awaiting_review": "processing",
    "succeeded": "completed",
    "failed": "failed",
    "canceled": "canceled",
}
DEFAULT_REVIEW_STATUS = "technical_only"


def derive(case_id: uuid.UUID, suffix: str) -> uuid.UUID:
    return uuid.uuid5(LEGACY_NS, f"{case_id}/{suffix}")


def kebab(value: str | None) -> str:
    """Legacy backend name as a kebab identifier ('unknown' when nothing is left)."""
    text = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:55].strip("-")
    return text if re.match(KEBAB, text) else "unknown"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def naive_utc(value: datetime | None) -> datetime | None:
    """Timestamp in the legacy representation (naive UTC)."""
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def study_status(run: PipelineRun | None) -> str:
    return STUDY_STATUS[run.status] if run is not None else NO_RUN_STATUS


def relative(storage: LocalStudyStorage, path: Path | str | None) -> str | None:
    """Path as stored in the new tables (relative to the storage root)."""
    if not path:
        return None
    return storage.relative_path(Path(path))


def absolute(storage: LocalStudyStorage, rel: str | None) -> Path | None:
    """Inverse of :func:`relative`; refuses anything that leaves the storage root."""
    if not rel:
        return None
    try:
        storage.resolve_relative(rel)
    except ValueError:
        return None
    return storage.root / rel


# ------------------------------------------------------------------------- lookup


def visible_case(db: Session, case_id: uuid.UUID, user: User) -> Case:
    """The case behind a legacy study id; 404 ``study_not_found`` as before."""
    case = db.get(Case, case_id)
    if case is None or case.deleted_at is not None:
        raise api_error(ErrorCode.STUDY_NOT_FOUND)
    require_study_access(case.owner_user_id, user)
    return case


def visible_cases(db: Session, user: User) -> list[Case]:
    query = (
        select(Case).where(Case.deleted_at.is_(None)).order_by(Case.created_at.desc())
    )
    if user.role != UserRole.admin.value:
        query = query.where(Case.owner_user_id == user.id)
    return list(db.scalars(query))


def latest_run(db: Session, case_id: uuid.UUID) -> PipelineRun | None:
    return db.scalar(
        select(PipelineRun)
        .where(PipelineRun.case_id == case_id)
        .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
        .limit(1)
    )


def run_steps(db: Session, run_id: uuid.UUID) -> list[StepRun]:
    """Attempts of a run, newest first (the order of the legacy job list)."""
    return list(
        db.scalars(
            select(StepRun)
            .where(StepRun.pipeline_run_id == run_id)
            .order_by(StepRun.queued_at.desc(), StepRun.attempt.desc())
        )
    )


def latest_step(db: Session, run: PipelineRun | None) -> StepRun | None:
    if run is None:
        return None
    steps = run_steps(db, run.id)
    return steps[0] if steps else None


def current_report(db: Session, run: PipelineRun | None) -> Report | None:
    """The newest report of the run (legacy runs have at most one)."""
    if run is None:
        return None
    return db.scalar(
        select(Report)
        .where(Report.pipeline_run_id == run.id)
        .order_by(Report.created_at.desc(), Report.id.desc())
        .limit(1)
    )


def outputs_zip(db: Session, run: PipelineRun | None) -> Artifact | None:
    if run is None:
        return None
    return db.scalar(
        select(Artifact)
        .where(
            Artifact.pipeline_run_id == run.id,
            Artifact.artefact_type == LEGACY_ZIP_TYPE,
        )
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        .limit(1)
    )


def report_pdf_path(
    db: Session, storage: LocalStudyStorage, report: Report | None
) -> Path | None:
    if report is None:
        return None
    artifact = db.get(Artifact, report.pdf_artifact_id)
    return absolute(storage, artifact.storage_path) if artifact else None


# ---------------------------------------------------------------------- read models


@dataclass
class _CaseFacts:
    run: PipelineRun | None
    upload: InputFile | None  # legacy_upload input, else the first input
    has_pdf: bool
    has_zip: bool


def _facts(db: Session, cases: Iterable[Case]) -> dict[uuid.UUID, _CaseFacts]:
    """Everything the study models need, in a fixed number of queries."""
    ids = [case.id for case in cases]
    if not ids:
        return {}
    runs: dict[uuid.UUID, PipelineRun] = {}
    for run in db.scalars(
        select(PipelineRun)
        .where(PipelineRun.case_id.in_(ids))
        .order_by(PipelineRun.created_at.desc(), PipelineRun.id.desc())
    ):
        runs.setdefault(run.case_id, run)
    uploads: dict[uuid.UUID, InputFile] = {}
    for item in db.scalars(
        select(InputFile)
        .where(InputFile.case_id.in_(ids))
        .order_by(InputFile.created_at, InputFile.role)
    ):
        current = uploads.get(item.case_id)
        if current is None or (
            item.role == LEGACY_UPLOAD and current.role != LEGACY_UPLOAD
        ):
            uploads[item.case_id] = item
    run_ids = [run.id for run in runs.values()]
    with_report = set(
        db.scalars(
            select(Report.pipeline_run_id).where(Report.pipeline_run_id.in_(run_ids))
        )
    ) if run_ids else set()
    with_zip = set(
        db.scalars(
            select(Artifact.pipeline_run_id).where(
                Artifact.pipeline_run_id.in_(run_ids),
                Artifact.artefact_type == LEGACY_ZIP_TYPE,
            )
        )
    ) if run_ids else set()
    out = {}
    for case_id in ids:
        run = runs.get(case_id)
        out[case_id] = _CaseFacts(
            run=run,
            upload=uploads.get(case_id),
            has_pdf=run is not None and run.id in with_report,
            has_zip=run is not None and run.id in with_zip,
        )
    return out


def original_filename(case: Case, upload: InputFile | None) -> str:
    """The upload's file name for legacy uploads (kept for this release only in
    ``legacy_attributes``); ``<role><extension>`` of the first input for cases created
    through /api/v1 (their file names are never stored)."""
    name = (case.legacy_attributes or {}).get("original_filename")
    if name:
        return name
    if upload is not None:
        suffixes = "".join(PurePosixPath(upload.storage_path).suffixes[-2:])
        return f"{upload.role}{suffixes}"
    return f"sub-{case.subject_label}"


def _updated_at(case: Case, run: PipelineRun | None) -> datetime:
    values = [naive_utc(case.updated_at)]
    if run is not None:
        values.append(naive_utc(run.updated_at))
    return max(v for v in values if v is not None)


def _study_read(case: Case, facts: _CaseFacts) -> StudyRead:
    attrs = case.legacy_attributes or {}
    run = facts.run
    upload = facts.upload
    return StudyRead(
        id=case.id,
        owner_user_id=case.owner_user_id,
        original_filename=original_filename(case, upload),
        status=study_status(run),
        created_at=naive_utc(case.created_at),
        updated_at=_updated_at(case, run),
        processing_started_at=naive_utc(run.started_at) if run else None,
        processing_finished_at=naive_utc(run.finished_at) if run else None,
        deleted_at=naive_utc(case.deleted_at),
        keep_forever=case.keep_forever,
        error_message=run.failure_message if run else None,
        processor_name=attrs.get("processor_name"),
        processor_version=attrs.get("processor_version"),
        processor_backend=attrs.get("processor_backend"),
        bids_subject_id=attrs.get("bids_subject_id"),
        file_size=upload.size_bytes if upload else None,
        checksum=upload.sha256 if upload else None,
        has_pdf=facts.has_pdf,
        has_output_zip=facts.has_zip,
        processing_warnings=attrs.get("processing_warnings"),
        clinical_review_status=(
            run.legacy_clinical_review_status if run else None
        )
        or DEFAULT_REVIEW_STATUS,
    )


def study_reads(db: Session, cases: list[Case]) -> list[StudyRead]:
    facts = _facts(db, cases)
    return [_study_read(case, facts[case.id]) for case in cases]


def study_read(db: Session, case: Case) -> StudyRead:
    return study_reads(db, [case])[0]


def status_read(db: Session, case: Case) -> StudyStatusRead:
    read = study_read(db, case)
    return StudyStatusRead(
        id=read.id,
        status=read.status,
        error_message=read.error_message,
        has_pdf=read.has_pdf,
        has_output_zip=read.has_output_zip,
        processing_warnings=read.processing_warnings,
        clinical_review_status=read.clinical_review_status,
        updated_at=read.updated_at,
    )


def job_read(
    step: StepRun, case_id: uuid.UUID, storage: LocalStudyStorage
) -> ProcessingJobRead:
    state = step.executor_state if isinstance(step.executor_state, dict) else {}
    log_path = absolute(storage, step.log_path)
    return ProcessingJobRead(
        id=step.id,
        study_id=case_id,
        celery_task_id=step.orchestrator_task_id,
        status=STUDY_STATUS[step.status],
        queued_at=naive_utc(step.queued_at),
        started_at=naive_utc(step.started_at),
        finished_at=naive_utc(step.finished_at),
        retry_count=step.attempt - 1,
        worker_name=state.get("worker_name"),
        exit_code=step.exit_code,
        log_path=str(log_path) if log_path else None,
        error_message=step.failure_message,
    )
