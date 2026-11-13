import socket
import signal
from datetime import datetime
from pathlib import Path
from uuid import UUID

from celery.utils.log import get_task_logger
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.processing_job import ProcessingJob
from app.models.study import Study, StudyStatus
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


@celery_app.task(name="worker.tasks.process_study", bind=True, max_retries=0)
def process_study(self, study_id: str, job_id: str) -> None:
    settings = get_settings()
    storage = LocalStudyStorage(settings.storage_root)
    db = SessionLocal()
    study_uuid = UUID(study_id)
    job_uuid = UUID(job_id)
    previous_signal_handlers = None

    try:
        study = db.get(Study, study_uuid)
        job = db.get(ProcessingJob, job_uuid)
        if not study or not job:
            logger.error("Study or job not found: %s %s", study_id, job_id)
            return
        if (
            study.deleted_at
            or study.status != StudyStatus.queued
            or job.status != StudyStatus.queued.value
        ):
            logger.info(
                "Skipping study %s with status %s and job status %s",
                study_id,
                study.status,
                job.status,
            )
            return

        now = datetime.utcnow()
        study.status = StudyStatus.processing
        study.processing_started_at = now
        study.error_message = None
        job.status = StudyStatus.processing.value
        job.started_at = now
        job.worker_name = socket.gethostname()
        record_event(db, "processing_started", study.id, {"job_id": str(job.id)})
        db.commit()
        previous_signal_handlers = _install_cancel_signal_handlers()

        processor_backend = study.processor_backend or settings.processor_backend
        adapter = create_processor_adapter(
            backend=processor_backend,
            processor_command=settings.processor_command,
            compneuro_command=settings.compneuro_command,
            compneuro_project_mount=settings.compneuro_project_mount,
            timeout_seconds=settings.processing_timeout_seconds,
        )

        result = adapter.run(
            input_dir=storage.input_dir(study.id),
            output_dir=storage.output_dir(study.id),
            study_id=str(study.id),
            logs_dir=storage.logs_dir(study.id),
            bids_project_dir=storage.bids_project_dir(study.id),
            runtime_project_dir=storage.runtime_project_dir(study.id),
        )

        db.refresh(study)
        db.refresh(job)
        if (
            study.status == StudyStatus.canceled
            or job.status == StudyStatus.canceled.value
            or _cancellation_requested(job)
        ):
            logger.info("Study %s was canceled while processing", study_id)
            _mark_study_canceled(study, job, "Cancelado durante el procesamiento")
            db.commit()
            return

        finished = datetime.utcnow()
        job.finished_at = finished
        job.exit_code = result.exit_code
        job.log_path = str(result.log_path)
        study.processing_finished_at = finished
        study.output_path = str(storage.output_dir(study.id))
        study.preproc_output_path = (
            str(result.preproc_path)
            if result.preproc_path
            else study.preproc_output_path
        )
        study.processing_warnings = None

        if result.success:
            pdf_path = result.pdf_path
            output_zip_path = result.output_zip_path
            warnings = list(result.warnings)
            if processor_backend.strip().lower() == "compneuro":
                rendered_outputs = []
                if settings.generate_rendered_png and result.preproc_path:
                    rendered_outputs = render_nifti_outputs(
                        result.preproc_path,
                        storage.rendered_png_dir(study.id),
                        renderer=settings.nifti_renderer,
                        max_files=settings.nifti_render_max_files,
                        timeout_seconds=settings.nifti_render_timeout_seconds,
                        log_path=storage.logs_dir(study.id) / "rendering.log",
                    )
                    study.rendered_png_dir = str(storage.rendered_png_dir(study.id))
                    failed_renders = [
                        artifact
                        for artifact in rendered_outputs
                        if not artifact.success
                    ]
                    warnings.extend(
                        f"No se pudo renderizar {artifact.display_name}: {artifact.error_message}"
                        for artifact in failed_renders
                    )
                    if not rendered_outputs:
                        warnings.append(
                            "No se encontraron ficheros NIfTI renderizables en Preproc"
                        )
                elif not settings.generate_rendered_png:
                    warnings.append(
                        "La generacion de PNG renderizados esta desactivada por configuracion"
                    )
                if settings.generate_technical_pdf:
                    report_filename = (
                        Path(settings.technical_report_filename).name
                        or "technical_report.pdf"
                    )
                    metadata = TechnicalReportMetadata(
                        study_id=str(study.id),
                        study_name=study.original_filename,
                        bids_subject_id=study.bids_subject_id,
                        uploaded_at=study.created_at,
                        processed_at=finished,
                        pipeline_name=study.processor_name,
                        pipeline_version=study.processor_version,
                        processor_backend=study.processor_backend,
                        logical_output_path=f"data/studies/{study.id}/output",
                    )
                    pdf_path = write_technical_pdf_report(
                        storage.technical_report_path(
                            study.id, filename=report_filename
                        ),
                        metadata=metadata,
                        rendered_outputs=rendered_outputs,
                        output_files=_relative_paths(
                            result.output_files, storage.output_dir(study.id)
                        ),
                        warnings=warnings,
                    )
                if settings.generate_output_zip:
                    output_zip_path = write_outputs_zip(
                        storage.output_dir(study.id), storage.output_zip_path(study.id)
                    )
                    study.output_zip_path = str(output_zip_path)
            study.status = StudyStatus.completed
            study.pdf_path = str(pdf_path) if pdf_path else None
            if output_zip_path:
                study.output_zip_path = str(output_zip_path)
            if warnings:
                study.processing_warnings = "\n".join(warnings)
            if _cancellation_requested_in_db(db, job.id):
                logger.info("Study %s was canceled before completion commit", study_id)
                _mark_study_canceled(study, job, "Cancelado durante el procesamiento")
                db.commit()
                return
            job.status = StudyStatus.completed.value
            record_event(
                db,
                "processing_completed",
                study.id,
                {
                    "pdf_path": str(pdf_path) if pdf_path else None,
                    "output_zip_path": str(output_zip_path)
                    if output_zip_path
                    else None,
                    "output_count": len(result.output_files),
                    "warning_count": len(warnings),
                    "duration_seconds": result.duration_seconds,
                },
            )
        else:
            if _cancellation_requested_in_db(db, job.id):
                logger.info("Study %s was canceled before failure commit", study_id)
                _mark_study_canceled(study, job, "Cancelado durante el procesamiento")
                db.commit()
                return
            study.status = StudyStatus.failed
            study.error_message = (
                result.error_message or "Error desconocido durante el procesamiento"
            )
            job.status = StudyStatus.failed.value
            job.error_message = study.error_message
            record_event(
                db,
                "processing_failed",
                study.id,
                {"error": study.error_message, "exit_code": result.exit_code},
            )

        db.commit()
        _notify_processing_finished(db, study, settings)
    except ProcessorCanceled as exc:
        db.rollback()
        logger.info("Processing canceled for study %s", study_id)
        study = db.get(Study, study_uuid)
        job = db.get(ProcessingJob, job_uuid)
        if study and job and _cancellation_requested(job):
            _mark_study_canceled(study, job, str(exc) or "Procesamiento cancelado")
            db.commit()
            return
        if study:
            study.status = StudyStatus.failed
            study.error_message = "Procesamiento interrumpido por señal externa"
            study.processing_finished_at = datetime.utcnow()
        if job:
            job.status = StudyStatus.failed.value
            job.finished_at = datetime.utcnow()
            job.error_message = str(exc) or "Procesamiento interrumpido por señal externa"
        db.commit()
        if study:
            _notify_processing_finished(db, study, settings)
    except Exception as exc:
        db.rollback()
        logger.exception("Unexpected worker error for study %s", study_id)
        study = db.get(Study, study_uuid)
        job = db.get(ProcessingJob, job_uuid)
        if study:
            study.status = StudyStatus.failed
            study.error_message = "Error interno durante el procesamiento"
            study.processing_finished_at = datetime.utcnow()
        if job:
            job.status = StudyStatus.failed.value
            job.finished_at = datetime.utcnow()
            job.error_message = str(exc)
        db.commit()
        if study:
            _notify_processing_finished(db, study, settings)
    finally:
        if previous_signal_handlers:
            _restore_signal_handlers(previous_signal_handlers)
        db.close()


def _relative_paths(paths: list[Path], base_dir: Path) -> list[Path]:
    relative_paths = []
    for path in paths:
        try:
            relative_paths.append(path.relative_to(base_dir))
        except ValueError:
            relative_paths.append(Path(path.name))
    return relative_paths


def _cancellation_requested(job: ProcessingJob) -> bool:
    return job.cancel_requested_at is not None


def _cancellation_requested_in_db(db, job_id: UUID) -> bool:
    with db.no_autoflush:
        cancel_requested_at = db.scalar(
            select(ProcessingJob.cancel_requested_at).where(
                ProcessingJob.id == job_id
            )
        )
    return cancel_requested_at is not None


def _mark_study_canceled(
    study: Study, job: ProcessingJob, job_error_message: str
) -> None:
    now = datetime.utcnow()
    study.status = StudyStatus.canceled
    study.error_message = "Cancelado por el usuario"
    study.processing_finished_at = study.processing_finished_at or now
    job.status = StudyStatus.canceled.value
    job.finished_at = job.finished_at or now
    job.error_message = job_error_message


def _install_cancel_signal_handlers():
    previous_handlers = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGINT: signal.getsignal(signal.SIGINT),
    }

    def handle_cancel(signum, _frame):
        raise ProcessorCanceled(f"Procesamiento cancelado por señal {signum}")

    for signum in previous_handlers:
        signal.signal(signum, handle_cancel)
    return previous_handlers


def _restore_signal_handlers(previous_handlers) -> None:
    for signum, handler in previous_handlers.items():
        signal.signal(signum, handler)


def _notify_processing_finished(db, study: Study, settings) -> None:
    try:
        notify_processing_finished(db, study, settings)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("Notification dispatch failed for study %s", study.id)
