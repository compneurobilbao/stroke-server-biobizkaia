"""Put a legacy study into a given state, for the tests of the legacy aliases.

Before the legacy cut-over (migration 0012) the legacy API tests simulated the worker by
writing ``studies`` and ``processing_jobs`` rows. Those tables are frozen now: a legacy
study is a case, its status is the status of its latest run and its jobs are the step
attempts of that run (docs/data-model.md 10.4). These helpers write the same states into
the run tables, as the legacy worker task does, so that the HTTP assertions of the tests
stay exactly as they were.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from sqlalchemy import select

from app.core.config import get_settings
from app.models.artifact import LEGACY_REPORT_TEMPLATE, Artifact, Report
from app.models.case import Case
from app.models.pipeline_run import PipelineRun, StepRun
from app.services import legacy_studies

RUN_STATUS = {
    "queued": "queued",
    "processing": "running",
    "completed": "succeeded",
    "failed": "failed",
    "canceled": "canceled",
}
TERMINAL = {"succeeded", "failed", "canceled"}


def storage_root() -> Path:
    return Path(get_settings().storage_root)


def study_dir(study_id) -> Path:
    return storage_root() / str(study_id)


def latest(db, study_id) -> tuple[Case, PipelineRun, StepRun | None]:
    case = db.get(Case, UUID(str(study_id)))
    run = legacy_studies.latest_run(db, case.id)
    return case, run, legacy_studies.latest_step(db, run)


def set_state(
    session_factory,
    study_id,
    status: str | None = None,
    *,
    error: str | None = None,
    celery_task_id: str | None = None,
    created_days_ago: int | None = None,
    keep_forever: bool | None = None,
) -> None:
    """Set the legacy ``status`` (``queued``, ``processing``, ``completed``, ``failed``,
    ``canceled``) on the latest run and its latest attempt, plus the other fields."""
    db = session_factory()
    try:
        case, run, step = latest(db, study_id)
        now = datetime.now(timezone.utc)
        if status is not None:
            value = RUN_STATUS[status]
            run.status = value
            run.finished_at = now if value in TERMINAL else None
            if value == "running":
                run.started_at = now
            run.failure_code = "legacy_failure" if value == "failed" else None
            run.failure_message = error
            if step is not None:
                step.status = value
                step.outcome = value if value in TERMINAL else None
                step.finished_at = now if value in TERMINAL else None
                step.failure_message = error
        if celery_task_id is not None and step is not None:
            step.orchestrator_task_id = celery_task_id
        if created_days_ago is not None:
            case.created_at = now - timedelta(days=created_days_ago)
        if keep_forever is not None:
            case.keep_forever = keep_forever
        db.commit()
    finally:
        db.close()


def complete_with_pdf(session_factory, study_id, *, zip_: bool = False) -> Path:
    """What the worker leaves after a successful run: a PDF (and optionally a ZIP)
    artefact of the latest attempt and the ``legacy-technical`` report."""
    set_state(session_factory, study_id, "completed")
    root = storage_root()
    report_path = study_dir(study_id) / "output" / "reports" / "technical_report.pdf"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    files = [("technical_report", "legacy_technical_report_pdf", report_path)]
    if zip_:
        zip_path = study_dir(study_id) / "output" / "outputs.zip"
        zip_path.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
        files.append(("outputs_zip", "legacy_outputs_zip", zip_path))
    db = session_factory()
    try:
        case, run, step = latest(db, study_id)
        for port, art_type, path in files:
            artifact_id = legacy_studies.derive(case.id, f"artifact/{port}")
            db.add(
                Artifact(
                    id=artifact_id,
                    case_id=case.id,
                    pipeline_run_id=run.id,
                    step_run_id=step.id,
                    origin="legacy",
                    port=port,
                    artefact_type=art_type,
                    storage_path=path.relative_to(root).as_posix(),
                    size_bytes=path.stat().st_size,
                    synthetic=True,
                )
            )
            db.flush()
            if port == "technical_report":
                db.add(
                    Report(
                        id=legacy_studies.derive(case.id, "report"),
                        case_id=case.id,
                        pipeline_run_id=run.id,
                        step_run_id=step.id,
                        pdf_artifact_id=artifact_id,
                        template=LEGACY_REPORT_TEMPLATE,
                        language="es",
                    )
                )
        db.commit()
    finally:
        db.close()
    return report_path


def seed_legacy_study(
    session_factory,
    root: Path,
    owner_id,
    *,
    status: str = "queued",
    backend: str = "dummy",
    filename: str = "study.nii",
) -> tuple[UUID, UUID]:
    """The rows the legacy upload alias writes (a case, its ``legacy_upload`` input, a
    ``legacy`` run with its slot and one attempt), with the input file on disk.
    ``status`` is the legacy status of the run and of the attempt. Returns the study
    (case) id and the job (attempt) id, the arguments of ``process_study``."""
    from uuid import uuid4

    from app.models.case import LEGACY_UPLOAD, InputFile
    from app.models.pipeline_run import RunInput, RunSlot

    case_id, step_id = uuid4(), uuid4()
    input_path = root / str(case_id) / "input" / "original" / filename
    input_path.parent.mkdir(parents=True, exist_ok=True)
    input_path.write_text("synthetic", encoding="utf-8")
    now = datetime.now(timezone.utc)
    value = RUN_STATUS[status]
    terminal = value in TERMINAL
    db = session_factory()
    try:
        db.add(
            Case(
                id=case_id,
                owner_user_id=owner_id,
                subject_label=case_id.hex[:12],
                deployment_profile="legacy",
                legacy_attributes={
                    "original_filename": filename,
                    "processor_backend": backend,
                    "output_path": f"{case_id}/output",
                },
            )
        )
        db.flush()
        input_id = legacy_studies.derive(case_id, "input")
        db.add(
            InputFile(
                id=input_id,
                case_id=case_id,
                role=LEGACY_UPLOAD,
                artefact_type=LEGACY_UPLOAD,
                storage_path=input_path.relative_to(root).as_posix(),
                size_bytes=input_path.stat().st_size,
                uploaded_by_user_id=owner_id,
            )
        )
        db.add(
            PipelineRun(
                id=case_id,
                case_id=case_id,
                created_by_user_id=owner_id,
                pipeline_id="legacy",
                pipeline_version="0.0.0",
                platform_version="legacy",
                artefact_types_version="legacy",
                input_set="legacy-single-file",
                seed=0,
                status=value,
                finished_at=now if terminal else None,
                requested_implementations={},
                accounting_submitted_by=f"user:{owner_id}",
                accounting_submit_as="service-account",
                legacy_clinical_review_status="technical_only",
            )
        )
        db.flush()
        slot_id = legacy_studies.derive(case_id, "slot/legacy")
        db.add(RunInput(pipeline_run_id=case_id, role=LEGACY_UPLOAD, input_file_id=input_id))
        db.add(
            RunSlot(
                id=slot_id,
                pipeline_run_id=case_id,
                slot="legacy",
                position=0,
                optional=False,
                status="selected",
                implementation_id=legacy_studies.kebab(backend),
                step_name=f"legacy-{legacy_studies.kebab(backend)}",
                step_version="0.0.0",
                stub=backend == "dummy",
                chosen_by="deployment_default",
                effective_parameters={},
            )
        )
        db.flush()
        db.add(
            StepRun(
                id=step_id,
                pipeline_run_id=case_id,
                run_slot_id=slot_id,
                attempt=1,
                trigger="initial",
                status=value,
                outcome=value if terminal else None,
                finished_at=now if terminal else None,
                executor="subprocess",
                runtime="none",
            )
        )
        db.commit()
    finally:
        db.close()
    return case_id, step_id


def steps_of(db, study_id) -> list[StepRun]:
    return list(
        db.scalars(
            select(StepRun)
            .where(StepRun.pipeline_run_id == UUID(str(study_id)))
            .order_by(StepRun.attempt)
        )
    )
