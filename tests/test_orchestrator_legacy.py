"""Legacy single-step runs (card F0-15): unchanged behaviour plus dual writing.

``process_study`` keeps driving legacy studies exactly as before; in addition it
mirrors each study into the new tables as a one-slot ``legacy`` run with the ids
and mapping of the 0011 back-fill (docs/data-model.md 10.1 stage 1b, 10.3).
"""

from __future__ import annotations

import sys
import uuid

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.db.base import Base
from app.models import (
    Artifact,
    Case,
    InputFile,
    PipelineRun,
    ProcessingJob,
    Report,
    RunSlot,
    StepRun,
    Study,
    StudyStatus,
    User,
)
from orchestrator import legacy
from worker.tasks import process_study


@pytest.fixture()
def legacy_env(tmp_path, monkeypatch):
    db_path = tmp_path / "legacy.db"
    storage_root = tmp_path / "studies"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("PROCESSOR_BACKEND", "dummy")
    get_settings.cache_clear()
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)
    import worker.tasks as tasks

    monkeypatch.setattr(tasks, "SessionLocal", Session)

    def make_study(fail: bool):
        script = "external_processor/dummy_processor.py"
        flags = " --fail" if fail else ""
        monkeypatch.setenv(
            "PROCESSOR_COMMAND",
            f"{sys.executable} {script} --input {{input_dir}} --output {{output_dir}}"
            f" --study-id {{study_id}} --sleep 0{flags}",
        )
        get_settings.cache_clear()
        study_id, job_id, owner_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        input_dir = storage_root / str(study_id) / "input"
        input_dir.mkdir(parents=True)
        (input_dir / "study.nii").write_text("synthetic", encoding="utf-8")
        with Session() as db:
            db.add(
                User(
                    id=owner_id,
                    email=f"{owner_id.hex[:8]}@example.org",
                    full_name="Owner",
                    role="researcher",
                )
            )
            db.add(
                Study(
                    id=study_id,
                    owner_user_id=owner_id,
                    original_filename="study.nii",
                    stored_path=str(input_dir / "study.nii"),
                    output_path=str(storage_root / str(study_id) / "output"),
                    processor_backend="dummy",
                    status=StudyStatus.queued,
                )
            )
            db.add(
                ProcessingJob(
                    id=job_id, study_id=study_id, status=StudyStatus.queued.value
                )
            )
            db.commit()
        return study_id, job_id

    yield Session, make_study, storage_root
    get_settings.cache_clear()


def test_legacy_success_is_unchanged_and_mirrored(legacy_env):
    Session, make_study, _root = legacy_env
    study_id, job_id = make_study(fail=False)
    process_study.run(str(study_id), str(job_id))
    with Session() as db:
        study = db.get(Study, study_id)
        assert study.status == StudyStatus.completed and study.pdf_path
        case = db.get(Case, study_id)
        assert case.deployment_profile == "legacy" and case.legacy_study_id == study_id
        assert (
            db.get(InputFile, legacy.derive(study_id, "input")).role == "legacy_upload"
        )
        run = db.get(PipelineRun, study_id)
        assert (run.pipeline_id, run.status, run.input_set) == (
            "legacy",
            "succeeded",
            "legacy-single-file",
        )
        assert run.finished_at is not None
        slot = db.get(RunSlot, legacy.derive(study_id, "slot/legacy"))
        assert (slot.slot, slot.implementation_id, slot.stub) == (
            "legacy",
            "dummy",
            True,
        )
        step = db.get(StepRun, job_id)
        assert (step.attempt, step.trigger, step.status, step.outcome) == (
            1,
            "initial",
            "succeeded",
            "succeeded",
        )
        assert step.legacy_job_id == job_id and step.executor == "subprocess"
        pdf = db.get(Artifact, legacy.derive(study_id, "artifact/technical_report"))
        assert pdf.step_run_id == job_id and not pdf.storage_path.startswith("/")
        assert (
            db.get(Report, legacy.derive(study_id, "report")).template
            == "legacy-technical"
        )


def test_legacy_failure_then_retry_is_mirrored_as_attempts(legacy_env):
    Session, make_study, _root = legacy_env
    study_id, job_id = make_study(fail=True)
    process_study.run(str(study_id), str(job_id))
    with Session() as db:
        assert db.get(Study, study_id).status == StudyStatus.failed
        run = db.get(PipelineRun, study_id)
        assert run.status == "failed" and run.failure_code == "legacy_failure"
        assert db.get(StepRun, job_id).outcome == "failed"
        # retry as the legacy endpoint does it
        study = db.get(Study, study_id)
        study.status = StudyStatus.queued
        retry_id = uuid.uuid4()
        db.add(
            ProcessingJob(
                id=retry_id, study_id=study_id, status="queued", retry_count=1
            )
        )
        db.commit()
    process_study.run(str(study_id), str(retry_id))
    with Session() as db:
        run = db.get(PipelineRun, study_id)
        assert run.status == "failed" and run.resume_count == 1
        steps = db.scalars(
            select(StepRun)
            .where(StepRun.pipeline_run_id == study_id)
            .order_by(StepRun.attempt)
        ).all()
        assert [(s.id, s.attempt, s.trigger) for s in steps] == [
            (job_id, 1, "initial"),
            (retry_id, 2, "user_resume"),
        ]


def test_mirror_failure_never_changes_the_legacy_result(legacy_env, monkeypatch):
    Session, make_study, _root = legacy_env
    study_id, job_id = make_study(fail=False)

    def broken(*_args, **_kwargs):
        raise RuntimeError("mirror broken")

    monkeypatch.setattr(legacy, "mirror_study", broken)
    process_study.run(str(study_id), str(job_id))
    with Session() as db:
        assert db.get(Study, study_id).status == StudyStatus.completed
        assert db.get(ProcessingJob, job_id).status == "completed"
        assert db.get(PipelineRun, study_id) is None
