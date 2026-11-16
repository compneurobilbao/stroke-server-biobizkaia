"""Dual writing of legacy studies into the new run tables (docs/data-model.md 10.1, stage 1b).

Between this card and F0-30 the legacy tables (``studies``, ``processing_jobs``)
stay authoritative and the legacy upload -> process -> PDF path keeps its exact
behaviour. The worker's ``process_study`` task additionally mirrors every study
it processes into the new tables as a one-slot ``legacy`` pipeline run, with the
same derived identifiers and mapping as the 0011 back-fill (section 10.3):

- ``cases.id = pipeline_runs.id = studies.id``; ``step_runs.id = processing_jobs.id``;
- other ids ``uuid5(LEGACY_NS, "<study id>/<suffix>")``.

``mirror_study`` is an idempotent upsert of the study's current state; the
worker calls it after each legacy commit (start and final state). It runs in its
own transaction after the legacy state is committed, so a mirror failure is
logged and never changes the legacy result (stage 2 reconciles any lag).
"""

from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.artifact import LEGACY_REPORT_TEMPLATE, Artifact, Report
from app.models.case import LEGACY_UPLOAD, Case, InputFile
from app.models.formats import KEBAB, SHA256_HEX
from app.models.pipeline_run import (
    LEGACY_PIPELINE_ID,
    LEGACY_SLOT,
    PipelineRun,
    RunInput,
    RunSlot,
    StepRun,
)
from app.models.processing_job import ProcessingJob
from app.models.study import Study

log = logging.getLogger(__name__)

LEGACY_NS = uuid.uuid5(uuid.NAMESPACE_URL, "urn:stroke-server:legacy")
STATUS = {
    "uploaded": "queued",
    "queued": "queued",
    "processing": "running",
    "completed": "succeeded",
    "failed": "failed",
    "canceled": "canceled",
}
OUTCOME = {"succeeded": "succeeded", "failed": "failed", "canceled": "canceled"}
TERMINAL = ("succeeded", "failed", "canceled")


def derive(study_id: uuid.UUID, suffix: str) -> uuid.UUID:
    return uuid.uuid5(LEGACY_NS, f"{study_id}/{suffix}")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _kebab(value: str | None) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:55].strip("-")
    return text if re.match(KEBAB, text) else "unknown"


def _relative(value: str | None, root: Path) -> str | None:
    if not value:
        return None
    path = Path(os.path.normpath(value))
    for base, candidate in ((root, path), (root.resolve(), path.resolve())):
        try:
            rel = candidate.relative_to(base)
        except ValueError:
            continue
        if ".." not in rel.parts:
            return rel.as_posix()
    return None


def _status(value) -> str:
    return STATUS[getattr(value, "value", value)]


def mirror_study(db: Session, study_id: uuid.UUID, storage_root: Path) -> None:
    """Upsert the new-model rows of one legacy study from its current state. Does not commit."""
    study = db.get(Study, study_id)
    if study is None:
        return
    jobs = db.scalars(
        select(ProcessingJob)
        .where(ProcessingJob.study_id == study_id)
        .order_by(ProcessingJob.queued_at, ProcessingJob.id)
    ).all()
    sid, owner = study.id, study.owner_user_id
    status = _status(study.status)
    terminal = status in TERMINAL
    rel = lambda v: _relative(v, storage_root)  # noqa: E731
    stub = study.processor_backend == "dummy"

    case = db.get(Case, sid)
    if case is None:
        case = Case(
            id=sid,
            owner_user_id=owner,
            subject_label=sid.hex[:12],
            deployment_profile="legacy",
            created_at=_utc(study.created_at),
            legacy_study_id=sid,
        )
        db.add(case)
    case.deleted_at = _utc(study.deleted_at)
    case.keep_forever = bool(study.keep_forever)
    case.legacy_attributes = {
        "original_filename": study.original_filename,
        "bids_subject_id": study.bids_subject_id,
        "processor_backend": study.processor_backend,
        "processor_name": study.processor_name,
        "processor_version": study.processor_version,
        "container_image": study.container_image,
        "processing_warnings": study.processing_warnings,
        "bids_path": rel(study.bids_path),
        "preproc_output_path": rel(study.preproc_output_path),
        "rendered_png_dir": rel(study.rendered_png_dir),
        "output_path": rel(study.output_path),
    }

    input_id = derive(sid, "input")
    stored = rel(study.stored_path)
    if db.get(InputFile, input_id) is None and stored:
        checksum = (study.checksum or "").lower()
        db.add(
            InputFile(
                id=input_id,
                case_id=sid,
                role=LEGACY_UPLOAD,
                artefact_type=LEGACY_UPLOAD,
                storage_path=stored,
                sha256=checksum if re.match(SHA256_HEX, checksum) else None,
                size_bytes=study.file_size if (study.file_size or 0) >= 0 else None,
                uploaded_by_user_id=owner,
                created_at=_utc(study.created_at),
            )
        )
    db.flush()

    latest = jobs[-1] if jobs else None
    run = db.get(PipelineRun, sid)
    if run is None:
        run = PipelineRun(
            id=sid,
            case_id=sid,
            created_by_user_id=owner,
            pipeline_id=LEGACY_PIPELINE_ID,
            pipeline_version="0.0.0",
            platform_version="legacy",
            artefact_types_version="legacy",
            input_set="legacy-single-file",
            seed=0,
            status=status,
            requested_implementations={},
            accounting_submitted_by=f"user:{owner}",
            accounting_submit_as="service-account",
            created_at=_utc(study.created_at),
        )
        db.add(run)
    run.status = status
    run.started_at = _utc(study.processing_started_at)
    run.finished_at = (
        (_utc(study.processing_finished_at) or _utc(study.updated_at))
        if terminal
        else None
    )
    run.cancel_requested_at = _utc(latest.cancel_requested_at) if latest else None
    run.resume_count = max(len(jobs) - 1, 0)
    run.failure_code = "legacy_failure" if status == "failed" else None
    run.failure_message = (
        study.error_message if status in ("failed", "canceled") else None
    )
    run.legacy_clinical_review_status = study.clinical_review_status
    db.flush()
    if (
        db.get(InputFile, input_id) is not None
        and db.get(RunInput, (sid, LEGACY_UPLOAD)) is None
    ):
        db.add(
            RunInput(pipeline_run_id=sid, role=LEGACY_UPLOAD, input_file_id=input_id)
        )

    slot_id = derive(sid, "slot/legacy")
    if db.get(RunSlot, slot_id) is None:
        impl = _kebab(study.processor_backend)
        db.add(
            RunSlot(
                id=slot_id,
                pipeline_run_id=sid,
                slot=LEGACY_SLOT,
                position=0,
                optional=False,
                status="selected",
                implementation_id=impl,
                step_name=f"legacy-{impl}",
                step_version="0.0.0",
                stub=stub,
                chosen_by="deployment_default",
                effective_parameters={},
            )
        )
    db.flush()

    for index, job in enumerate(jobs):
        _mirror_job(db, sid, slot_id, index, job, latest, jobs, rel)
        db.flush()  # sessions here may not autoflush; later jobs read attempt numbers

    target = next(
        (j for j in reversed(jobs) if _status(j.status) == "succeeded"), latest
    )
    if target is None or db.get(StepRun, target.id) is None:
        return
    for path, port, art_type in (
        (study.pdf_path, "technical_report", "legacy_technical_report_pdf"),
        (study.output_zip_path, "outputs_zip", "legacy_outputs_zip"),
    ):
        relative = rel(path)
        if not relative:
            continue
        artifact_id = derive(sid, f"artifact/{port}")
        artifact = db.get(Artifact, artifact_id)
        if artifact is None:
            artifact = Artifact(
                id=artifact_id,
                case_id=sid,
                pipeline_run_id=sid,
                origin="legacy",
                port=port,
                artefact_type=art_type,
                synthetic=stub,
            )
            db.add(artifact)
        artifact.step_run_id = target.id
        artifact.storage_path = relative
        db.flush()
        if port == "technical_report":
            report = db.get(Report, derive(sid, "report"))
            if report is None:
                db.add(
                    Report(
                        id=derive(sid, "report"),
                        case_id=sid,
                        pipeline_run_id=sid,
                        step_run_id=target.id,
                        pdf_artifact_id=artifact_id,
                        template=LEGACY_REPORT_TEMPLATE,
                        language="es",
                    )
                )
            else:
                report.step_run_id = target.id


def _mirror_job(db, sid, slot_id, index, job, latest, jobs, rel) -> None:
    status = _status(job.status)
    failure_code = None
    if status in ("queued", "running") and job is not latest:
        status, failure_code = "failed", "legacy_stale_job"
    finished = None
    if status in TERMINAL:
        finished = _utc(job.finished_at)
        if finished is None and failure_code:
            finished = _utc(jobs[index + 1].queued_at)
        finished = finished or _utc(job.started_at) or _utc(job.queued_at)
    values = dict(
        status=status,
        outcome=OUTCOME.get(status),
        started_at=_utc(job.started_at),
        finished_at=finished,
        exit_code=job.exit_code,
        failure_code=failure_code,
        failure_message=job.error_message,
        log_path=rel(job.log_path),
        orchestrator_task_id=job.celery_task_id,
        cancel_requested_at=_utc(job.cancel_requested_at),
        executor_state={"worker_name": job.worker_name} if job.worker_name else None,
    )
    step = db.get(StepRun, job.id)
    if step is None:
        if status in ("queued", "running"):
            # A crashed earlier attempt may still look live: close it first so that
            # uq_step_runs_one_active admits this one.
            db.execute(
                update(StepRun)
                .where(
                    StepRun.run_slot_id == slot_id,
                    StepRun.id != job.id,
                    StepRun.status.in_(("queued", "running")),
                )
                .values(
                    status="failed",
                    outcome="failed",
                    failure_code="legacy_stale_job",
                    finished_at=_utc(job.queued_at),
                )
                .execution_options(synchronize_session=False)
            )
        attempt = index + 1
        taken = set(
            db.scalars(select(StepRun.attempt).where(StepRun.run_slot_id == slot_id))
        )
        while attempt in taken:
            attempt += 1
        db.add(
            StepRun(
                id=job.id,
                pipeline_run_id=sid,
                run_slot_id=slot_id,
                attempt=attempt,
                trigger="initial" if attempt == 1 else "user_resume",
                executor="subprocess",
                runtime="none",
                queued_at=_utc(job.queued_at),
                legacy_job_id=job.id,
                **values,
            )
        )
    else:
        for key, value in values.items():
            setattr(step, key, value)


def mirror_safely(
    db: Session,
    study_id: uuid.UUID,
    storage_root: Path,
    reraise: tuple[type[BaseException], ...] = (),
) -> bool:
    """Mirror and commit; on any error roll back, log and return False.

    Exceptions in ``reraise`` (the worker passes its cancellation exception,
    raised by its SIGTERM handler) are rolled back and propagated, so that a
    cancel arriving during the mirror is handled by the legacy code as before.
    """
    try:
        mirror_study(db, study_id, storage_root)
        db.commit()
        return True
    except reraise:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        log.exception("Could not mirror legacy study %s into the run tables", study_id)
        return False
