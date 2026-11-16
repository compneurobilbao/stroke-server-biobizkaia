"""Celery tasks: the legacy processing task and the pipeline orchestrator tasks.

``process_study`` runs one attempt of a legacy run (docs/data-model.md 10.1 stage 2).
Since migration 0012 it reads and writes the case model directly (``cases``,
``pipeline_runs``, ``step_runs``, ``artifacts``, ``reports``); ``studies`` and
``processing_jobs`` are frozen and nothing writes them. The arguments keep their legacy
names: the study id is the case id and the job id is the id of the step attempt.
Behaviour is that of the legacy task: the adapter runs in this process; cancellation is
a Celery revoke with SIGTERM, turned into ``ProcessorCanceled`` and honoured only when
``cancel_requested_at`` of the attempt is set; PNG rendering, technical PDF and ZIP for
the compneuro backend.
"""

import hashlib
import socket
import signal
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

from celery.signals import worker_ready
from celery.utils.log import get_task_logger
from sqlalchemy import select, update

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.artifact import LEGACY_REPORT_TEMPLATE, Artifact, Report
from app.models.case import Case
from app.models.pipeline_run import TERMINAL_STATUSES, PipelineRun, StepRun
from app.services import legacy_studies as legacy
from app.services.audit import record_event
from app.services.notifications import notify_processing_finished
from app.services.storage import LocalStudyStorage
from processor_adapter import create_processor_adapter
from processor_adapter.cancellation import ProcessorCanceled
from processor_adapter.nifti_renderer import render_nifti_outputs
from processor_adapter.output_packager import write_outputs_zip
from processor_adapter.technical_pdf_report import (
    TechnicalReportMetadata,
    write_technical_pdf_report,
)
from worker.celery_app import celery_app

logger = get_task_logger(__name__)

PORT_PDF = "technical_report"
PORT_ZIP = "outputs_zip"


@celery_app.task(name="worker.tasks.process_study", bind=True, max_retries=0)
def process_study(self, study_id: str, job_id: str) -> None:
    settings = get_settings()
    storage = LocalStudyStorage(settings.storage_root)
    db = SessionLocal()
    case_uuid = UUID(study_id)
    step_uuid = UUID(job_id)
    previous_signal_handlers = None

    try:
        case = db.get(Case, case_uuid)
        step = db.get(StepRun, step_uuid)
        run = db.get(PipelineRun, step.pipeline_run_id) if step else None
        if not case or not step or not run or run.case_id != case.id:
            logger.error("Study or job not found: %s %s", study_id, job_id)
            return
        if case.deleted_at or run.status != "queued" or step.status != "queued":
            logger.info(
                "Skipping study %s with run status %s and job status %s",
                study_id,
                run.status,
                step.status,
            )
            return

        now = _now()
        # Compare-and-set start: a cancel or a delete that got there first wins.
        run_started = db.execute(
            update(PipelineRun)
            .where(PipelineRun.id == run.id, PipelineRun.status == "queued")
            .values(
                status="running", started_at=now, failure_message=None, updated_at=now
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        started_values = {
            "status": "running",
            "started_at": now,
            "executor_state": {"worker_name": socket.gethostname()},
        }
        if self.request.id:
            # The task that runs the attempt is the one a cancel must revoke, even if
            # a recovery re-enqueued the attempt meanwhile (worker/legacy_recovery.py).
            started_values["orchestrator_task_id"] = self.request.id
        step_started = db.execute(
            update(StepRun)
            .where(StepRun.id == step.id, StepRun.status == "queued")
            .values(**started_values)
            .execution_options(synchronize_session=False)
        ).rowcount
        if run_started != 1 or step_started != 1:
            db.rollback()
            logger.info("Study %s is no longer queued; skipping", study_id)
            return
        record_event(
            db,
            "processing_started",
            details={"job_id": str(step.id)},
            case_id=case.id,
            pipeline_run_id=run.id,
        )
        db.commit()
        previous_signal_handlers = _install_cancel_signal_handlers()
        db.refresh(run)
        db.refresh(step)
        attrs = dict(case.legacy_attributes or {})

        processor_backend = attrs.get("processor_backend") or settings.processor_backend
        adapter = create_processor_adapter(
            backend=processor_backend,
            processor_command=settings.processor_command,
            compneuro_command=settings.compneuro_command,
            compneuro_project_mount=settings.compneuro_project_mount,
            timeout_seconds=settings.processing_timeout_seconds,
        )

        result = adapter.run(
            input_dir=storage.input_dir(case.id),
            output_dir=storage.output_dir(case.id),
            study_id=str(case.id),
            logs_dir=storage.logs_dir(case.id),
            bids_project_dir=storage.bids_project_dir(case.id),
            runtime_project_dir=storage.runtime_project_dir(case.id),
        )

        db.refresh(run)
        db.refresh(step)
        if (
            run.status == "canceled"
            or step.status == "canceled"
            or _cancellation_requested(step)
        ):
            logger.info("Study %s was canceled while processing", study_id)
            _mark_canceled(run, step, "Canceled during processing")
            db.commit()
            return

        finished = _now()
        step.exit_code = result.exit_code
        step.log_path = _relative(storage, result.log_path)
        attrs["output_path"] = _relative(storage, storage.output_dir(case.id))
        if result.preproc_path:
            attrs["preproc_output_path"] = _relative(storage, result.preproc_path)
        attrs["processing_warnings"] = None

        if result.success:
            pdf_path = result.pdf_path
            output_zip_path = result.output_zip_path
            warnings = list(result.warnings)
            if processor_backend.strip().lower() == "compneuro":
                rendered_outputs = []
                if settings.generate_rendered_png and result.preproc_path:
                    rendered_outputs = render_nifti_outputs(
                        result.preproc_path,
                        storage.rendered_png_dir(case.id),
                        renderer=settings.nifti_renderer,
                        max_files=settings.nifti_render_max_files,
                        timeout_seconds=settings.nifti_render_timeout_seconds,
                        log_path=storage.logs_dir(case.id) / "rendering.log",
                    )
                    attrs["rendered_png_dir"] = _relative(
                        storage, storage.rendered_png_dir(case.id)
                    )
                    failed_renders = [
                        artifact
                        for artifact in rendered_outputs
                        if not artifact.success
                    ]
                    warnings.extend(
                        f"Could not render {artifact.display_name}: {artifact.error_message}"
                        for artifact in failed_renders
                    )
                    if not rendered_outputs:
                        warnings.append(
                            "No renderable NIfTI files found in Preproc"
                        )
                elif not settings.generate_rendered_png:
                    warnings.append(
                        "Rendered PNG generation is disabled by configuration"
                    )
                if settings.generate_technical_pdf:
                    report_filename = (
                        Path(settings.technical_report_filename).name
                        or "technical_report.pdf"
                    )
                    metadata = TechnicalReportMetadata(
                        study_id=str(case.id),
                        study_name=attrs.get("original_filename"),
                        bids_subject_id=attrs.get("bids_subject_id"),
                        uploaded_at=legacy.naive_utc(case.created_at),
                        processed_at=legacy.naive_utc(finished),
                        pipeline_name=attrs.get("processor_name"),
                        pipeline_version=attrs.get("processor_version"),
                        processor_backend=attrs.get("processor_backend"),
                        logical_output_path=f"data/studies/{case.id}/output",
                    )
                    pdf_path = write_technical_pdf_report(
                        storage.technical_report_path(
                            case.id, filename=report_filename
                        ),
                        metadata=metadata,
                        rendered_outputs=rendered_outputs,
                        output_files=_relative_paths(
                            result.output_files, storage.output_dir(case.id)
                        ),
                        warnings=warnings,
                    )
                if settings.generate_output_zip:
                    output_zip_path = write_outputs_zip(
                        storage.output_dir(case.id), storage.output_zip_path(case.id)
                    )
            if warnings:
                attrs["processing_warnings"] = "\n".join(warnings)
            case.legacy_attributes = attrs
            if _cancellation_requested_in_db(db, step.id):
                logger.info("Study %s was canceled before completion commit", study_id)
                _mark_canceled(run, step, "Canceled during processing", finished)
                db.commit()
                return
            _finish(run, step, "succeeded", finished)
            db.flush()
            _store_outputs(db, storage, case, run, step, pdf_path, output_zip_path)
            record_event(
                db,
                "processing_completed",
                details={
                    "pdf_path": str(pdf_path) if pdf_path else None,
                    "output_zip_path": str(output_zip_path)
                    if output_zip_path
                    else None,
                    "output_count": len(result.output_files),
                    "warning_count": len(warnings),
                    "duration_seconds": result.duration_seconds,
                },
                case_id=case.id,
                pipeline_run_id=run.id,
            )
        else:
            case.legacy_attributes = attrs
            if _cancellation_requested_in_db(db, step.id):
                logger.info("Study %s was canceled before failure commit", study_id)
                _mark_canceled(run, step, "Canceled during processing", finished)
                db.commit()
                return
            error_message = result.error_message or "Unknown error during processing"
            _finish(run, step, "failed", finished, error_message, error_message)
            record_event(
                db,
                "processing_failed",
                details={"error": error_message, "exit_code": result.exit_code},
                case_id=case.id,
                pipeline_run_id=run.id,
            )

        db.commit()
        _notify_processing_finished(db, case, run, settings)
    except ProcessorCanceled as exc:
        db.rollback()
        logger.info("Processing canceled for study %s", study_id)
        case, run, step = _reload(db, case_uuid, step_uuid)
        if run and step and _cancellation_requested(step):
            _mark_canceled(run, step, str(exc) or "Processing canceled")
            db.commit()
            return
        if case and run and step:
            _finish(
                run,
                step,
                "failed",
                _now(),
                "Processing interrupted by an external signal",
                str(exc) or "Processing interrupted by an external signal",
            )
            db.commit()
            _notify_processing_finished(db, case, run, settings)
    except Exception as exc:
        db.rollback()
        logger.exception("Unexpected worker error for study %s", study_id)
        case, run, step = _reload(db, case_uuid, step_uuid)
        if case and run and step:
            _finish(
                run, step, "failed", _now(), "Internal error during processing", str(exc)
            )
            db.commit()
            _notify_processing_finished(db, case, run, settings)
    finally:
        if previous_signal_handlers:
            _restore_signal_handlers(previous_signal_handlers)
        db.close()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _relative(storage: LocalStudyStorage, path) -> str | None:
    try:
        return legacy.relative(storage, path)
    except ValueError:
        logger.warning("Path outside the storage root not recorded: %s", path)
        return None


def _reload(db, case_id: UUID, step_id: UUID):
    case = db.get(Case, case_id)
    step = db.get(StepRun, step_id)
    run = db.get(PipelineRun, step.pipeline_run_id) if step else None
    return case, run, step


def _finish(
    run: PipelineRun,
    step: StepRun,
    status: str,
    when: datetime,
    run_message: str | None = None,
    step_message: str | None = None,
) -> None:
    """Final state of the attempt and of the run. Rows already final are left alone,
    so that a late task never overwrites a newer outcome."""
    if step.status not in TERMINAL_STATUSES:
        step.status = status
        step.outcome = status
        step.finished_at = when
        if step_message is not None:
            step.failure_message = step_message
    if run.status not in TERMINAL_STATUSES:
        run.status = status
        run.finished_at = when
        run.updated_at = when
        run.failure_code = "legacy_failure" if status == "failed" else None
        if run_message is not None or status == "succeeded":
            run.failure_message = run_message


def _mark_canceled(
    run: PipelineRun,
    step: StepRun,
    job_error_message: str,
    when: datetime | None = None,
) -> None:
    now = when or _now()
    run.status = "canceled"
    run.failure_code = None
    run.failure_message = "Canceled by the user"
    run.finished_at = run.finished_at or now
    run.updated_at = now
    step.status = "canceled"
    step.outcome = "canceled"
    step.finished_at = step.finished_at or now
    step.failure_message = job_error_message


def _sha256(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _store_outputs(db, storage, case, run, step, pdf_path, zip_path) -> None:
    """The technical PDF and the ZIP as ``legacy`` artefacts of this attempt, plus the
    ``legacy-technical`` report, with the ids of the back-fill (data-model.md 10.2).
    Sizes and hashes are recorded so that the quota counts them."""
    stub = (case.legacy_attributes or {}).get("processor_backend") == "dummy"
    for path, port, art_type in (
        (pdf_path, PORT_PDF, legacy.LEGACY_PDF_TYPE),
        (zip_path, PORT_ZIP, legacy.LEGACY_ZIP_TYPE),
    ):
        relative = _relative(storage, path) if path else None
        if not relative:
            continue
        sha256 = size = None
        if Path(path).is_file():
            sha256, size = _sha256(Path(path))
        artifact_id = legacy.derive(case.id, f"artifact/{port}")
        artifact = db.get(Artifact, artifact_id)
        if artifact is None:
            artifact = Artifact(
                id=artifact_id,
                case_id=case.id,
                pipeline_run_id=run.id,
                origin="legacy",
                port=port,
                artefact_type=art_type,
                synthetic=stub,
            )
            db.add(artifact)
        artifact.step_run_id = step.id
        artifact.storage_path = relative
        artifact.sha256 = sha256
        artifact.size_bytes = size
        db.flush()
        if port == PORT_PDF:
            report_id = legacy.derive(case.id, "report")
            report = db.get(Report, report_id)
            if report is None:
                db.add(
                    Report(
                        id=report_id,
                        case_id=case.id,
                        pipeline_run_id=run.id,
                        step_run_id=step.id,
                        pdf_artifact_id=artifact_id,
                        template=LEGACY_REPORT_TEMPLATE,
                        language="es",
                    )
                )
            else:
                report.step_run_id = step.id
            db.flush()


def _relative_paths(paths: list[Path], base_dir: Path) -> list[Path]:
    relative_paths = []
    for path in paths:
        try:
            relative_paths.append(path.relative_to(base_dir))
        except ValueError:
            relative_paths.append(Path(path.name))
    return relative_paths


def _cancellation_requested(step: StepRun) -> bool:
    return step.cancel_requested_at is not None


def _cancellation_requested_in_db(db, step_id: UUID) -> bool:
    with db.no_autoflush:
        cancel_requested_at = db.scalar(
            select(StepRun.cancel_requested_at).where(StepRun.id == step_id)
        )
    return cancel_requested_at is not None


def _install_cancel_signal_handlers():
    previous_handlers = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGINT: signal.getsignal(signal.SIGINT),
    }

    def handle_cancel(signum, _frame):
        raise ProcessorCanceled(f"Processing canceled by signal {signum}")

    for signum in previous_handlers:
        signal.signal(signum, handle_cancel)
    return previous_handlers


def _restore_signal_handlers(previous_handlers) -> None:
    for signum, handler in previous_handlers.items():
        signal.signal(signum, handler)


def _notify_processing_finished(db, case: Case, run: PipelineRun, settings) -> None:
    try:
        notify_processing_finished(db, case, run, settings)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Notification dispatch failed for study %s", case.id)


@celery_app.task(name="worker.tasks.recover_legacy_runs")
def recover_legacy_runs() -> dict:
    """Re-enqueue orphaned queued legacy attempts and fail attempts whose worker is gone
    (worker/legacy_recovery.py). Idempotent; scheduled at worker start-up and by beat."""
    from worker.legacy_recovery import (
        live_task_ids,
        recover_legacy_runs as recover,
        thresholds,
    )

    settings = get_settings()
    queued_after, running_after = thresholds(settings)

    def on_failed(db, case, run):
        _notify_processing_finished(db, case, run, settings)

    report = recover(
        SessionLocal,
        live_ids=live_task_ids(celery_app),
        enqueue=enqueue_attempt,
        queued_after=queued_after,
        running_after=running_after,
        execute=True,
        on_failed=on_failed,
    )
    if report.requeued or report.failed_lost:
        logger.warning("Legacy recovery: %s", report.as_dict())
    return report.as_dict()


def enqueue_attempt(case_id: UUID, step_id: UUID, task_id: str) -> None:
    process_study.apply_async(args=(str(case_id), str(step_id)), task_id=task_id)


@worker_ready.connect
def _schedule_legacy_recovery(sender=None, **_kwargs) -> None:
    """After a (re)start, look for legacy attempts left without a task once the worker
    is consuming. Several replicas may schedule it; the recovery is idempotent."""
    try:
        recover_legacy_runs.apply_async(countdown=60)
    except Exception:
        logger.exception("Could not schedule the legacy recovery")


# --------------------------------------------------------------- pipeline runs (F0-15)


def _orchestrator():
    from orchestrator.config import build_orchestrator

    settings = get_settings()
    return build_orchestrator(SessionLocal, settings.storage_root, settings=settings)


@celery_app.task(name="worker.tasks.run_pipeline", bind=True, max_retries=0)
def run_pipeline(self, run_id: str) -> str:
    """Drive one pipeline run to a final state (orchestrator.engine).

    Cancellation is requested through ``pipeline_runs.cancel_requested_at``, not
    by revoking this task: the orchestrator propagates it to the executor and
    records the outcome. ``self.request.id`` is the lease owner.
    """
    owner = self.request.id or f"local-{socket.gethostname()}"
    result = _orchestrator().drive(UUID(run_id), owner)
    logger.info("Pipeline run %s: %s", run_id, result.value)
    return result.value


@celery_app.task(name="worker.tasks.recover_pipeline_runs")
def recover_pipeline_runs() -> int:
    """Re-enqueue active runs whose driver stopped heartbeating (data-model.md 7.1)."""
    from orchestrator.config import config_from_env
    from orchestrator.engine import stale_runs

    db = SessionLocal()
    try:
        run_ids = stale_runs(db, config_from_env().lease_seconds)
    finally:
        db.close()
    for run_id in run_ids:
        run_pipeline.delay(str(run_id))
    return len(run_ids)
