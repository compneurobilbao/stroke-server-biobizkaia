from datetime import datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.db.base import Base
from app.models.notification import Notification
from app.models.processing_job import ProcessingJob
from app.models.study import Study, StudyStatus
from app.models.user import User, UserRole
from app.services.auth import hash_password
from processor_adapter.cancellation import ProcessorCanceled
from worker.tasks import process_study


def test_worker_marks_study_failed_when_script_fails(tmp_path, monkeypatch):
    db_path = tmp_path / "worker.db"
    storage_root = tmp_path / "studies"
    study_id = uuid4()
    job_id = uuid4()
    input_dir = storage_root / str(study_id) / "input"
    input_dir.mkdir(parents=True)
    (input_dir / "study.nii").write_text("dummy", encoding="utf-8")

    script = "external_processor/dummy_processor.py"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("PROCESSOR_BACKEND", "dummy")
    monkeypatch.setenv(
        "PROCESSOR_COMMAND",
        f"python {script} --input {{input_dir}} --output {{output_dir}} --study-id {{study_id}} --sleep 0 --fail",
    )
    get_settings.cache_clear()

    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)

    import worker.tasks as tasks

    monkeypatch.setattr(tasks, "SessionLocal", TestingSessionLocal)

    db = TestingSessionLocal()
    owner_id = uuid4()
    db.add(
        User(
            id=owner_id,
            email="worker-owner@example.org",
            full_name="Worker Owner",
            hashed_password=hash_password("secret-pass"),
            role=UserRole.researcher.value,
            is_active=True,
        )
    )
    db.add(
        Study(
            id=study_id,
            owner_user_id=owner_id,
            original_filename="study.nii",
            stored_path=str(input_dir / "study.nii"),
            output_path=str(storage_root / str(study_id) / "output"),
            status=StudyStatus.queued,
        )
    )
    db.add(ProcessingJob(id=job_id, study_id=study_id, status=StudyStatus.queued.value))
    db.commit()
    db.close()

    process_study.run(str(study_id), str(job_id))

    db = TestingSessionLocal()
    study = db.get(Study, study_id)
    job = db.get(ProcessingJob, job_id)
    assert study.status == StudyStatus.failed
    assert job.status == StudyStatus.failed.value
    assert study.error_message == "El procesador externo terminó con error"
    notification = db.query(Notification).one()
    assert notification.recipient_user_id == owner_id
    assert notification.event_type == "processing_failed"
    assert notification.email_status == "disabled"
    db.close()
    get_settings.cache_clear()


def test_worker_skips_canceled_study(tmp_path, monkeypatch):
    db_path = tmp_path / "worker.db"
    storage_root = tmp_path / "studies"
    study_id = uuid4()
    job_id = uuid4()
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("PROCESSOR_BACKEND", "dummy")
    get_settings.cache_clear()

    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)

    import worker.tasks as tasks

    monkeypatch.setattr(tasks, "SessionLocal", TestingSessionLocal)

    db = TestingSessionLocal()
    owner_id = uuid4()
    db.add(
        User(
            id=owner_id,
            email="worker-owner@example.org",
            full_name="Worker Owner",
            hashed_password=hash_password("secret-pass"),
            role=UserRole.researcher.value,
            is_active=True,
        )
    )
    db.add(
        Study(
            id=study_id,
            owner_user_id=owner_id,
            original_filename="study.nii",
            stored_path=str(storage_root / str(study_id) / "input" / "study.nii"),
            output_path=str(storage_root / str(study_id) / "output"),
            status=StudyStatus.canceled,
        )
    )
    db.add(
        ProcessingJob(id=job_id, study_id=study_id, status=StudyStatus.canceled.value)
    )
    db.commit()
    db.close()

    process_study.run(str(study_id), str(job_id))

    db = TestingSessionLocal()
    study = db.get(Study, study_id)
    job = db.get(ProcessingJob, job_id)
    assert study.status == StudyStatus.canceled
    assert job.status == StudyStatus.canceled.value
    assert job.started_at is None
    db.close()
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("flag_set", "error_message"),
    [
        (True, "Cancelación solicitada por el usuario"),
        (True, "arbitrary text unrelated to cancellation"),
        (False, "Cancelación solicitada por el usuario"),
    ],
)
def test_worker_cancellation_depends_only_on_flag(
    tmp_path, monkeypatch, flag_set, error_message
):
    db_path = tmp_path / "worker.db"
    storage_root = tmp_path / "studies"
    study_id = uuid4()
    job_id = uuid4()
    input_dir = storage_root / str(study_id) / "input"
    input_dir.mkdir(parents=True)
    (input_dir / "study.nii").write_text("dummy", encoding="utf-8")

    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("PROCESSOR_BACKEND", "dummy")
    get_settings.cache_clear()

    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)

    import worker.tasks as tasks

    monkeypatch.setattr(tasks, "SessionLocal", TestingSessionLocal)

    class CancelingAdapter:
        @staticmethod
        def run(**_kwargs):
            db = TestingSessionLocal()
            job = db.get(ProcessingJob, job_id)
            job.error_message = error_message
            if flag_set:
                job.cancel_requested_at = datetime.utcnow()
            db.commit()
            db.close()
            raise ProcessorCanceled("Procesamiento cancelado por señal 15")

    monkeypatch.setattr(
        tasks, "create_processor_adapter", lambda **_kwargs: CancelingAdapter()
    )

    db = TestingSessionLocal()
    owner_id = uuid4()
    db.add(
        User(
            id=owner_id,
            email="worker-owner@example.org",
            full_name="Worker Owner",
            hashed_password=hash_password("secret-pass"),
            role=UserRole.researcher.value,
            is_active=True,
        )
    )
    db.add(
        Study(
            id=study_id,
            owner_user_id=owner_id,
            original_filename="study.nii",
            stored_path=str(input_dir / "study.nii"),
            output_path=str(storage_root / str(study_id) / "output"),
            status=StudyStatus.queued,
        )
    )
    db.add(ProcessingJob(id=job_id, study_id=study_id, status=StudyStatus.queued.value))
    db.commit()
    db.close()

    process_study.run(str(study_id), str(job_id))

    db = TestingSessionLocal()
    study = db.get(Study, study_id)
    job = db.get(ProcessingJob, job_id)
    if flag_set:
        assert study.status == StudyStatus.canceled
        assert study.error_message == "Cancelado por el usuario"
        assert job.status == StudyStatus.canceled.value
        assert job.error_message == "Procesamiento cancelado por señal 15"
        assert db.query(Notification).count() == 0
    else:
        # Message text alone must never be treated as a cancellation.
        assert study.status == StudyStatus.failed
        assert job.status == StudyStatus.failed.value
    db.close()
    get_settings.cache_clear()
