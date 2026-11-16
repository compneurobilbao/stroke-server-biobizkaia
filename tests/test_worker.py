"""The legacy worker task over the case model (since migration 0012).

The legacy state of a study is the state of its ``legacy`` run and of the attempt the
task was given (docs/data-model.md 10.4); the behaviour asserted here is the legacy one.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models.notification import Notification
from app.models.pipeline_run import PipelineRun, StepRun
from app.models.user import User, UserRole
from app.services.auth import hash_password
from processor_adapter.cancellation import ProcessorCanceled
from tests import pgdb
from tests.legacy_state import seed_legacy_study
from worker.tasks import process_study


@pytest.fixture()
def worker_env(tmp_path, monkeypatch):
    storage_root = tmp_path / "studies"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.setenv("PROCESSOR_BACKEND", "dummy")
    engine = pgdb.new_engine(
        tmp_path, "worker.db", connect_args={"check_same_thread": False}
    )
    monkeypatch.setenv("DATABASE_URL", pgdb.database_url(engine))
    get_settings.cache_clear()
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

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
    db.commit()
    db.close()
    yield TestingSessionLocal, storage_root, owner_id
    get_settings.cache_clear()


def test_worker_marks_study_failed_when_script_fails(worker_env, monkeypatch):
    TestingSessionLocal, storage_root, owner_id = worker_env
    script = "external_processor/dummy_processor.py"
    monkeypatch.setenv(
        "PROCESSOR_COMMAND",
        f"python {script} --input {{input_dir}} --output {{output_dir}} --study-id {{study_id}} --sleep 0 --fail",
    )
    get_settings.cache_clear()
    study_id, job_id = seed_legacy_study(TestingSessionLocal, storage_root, owner_id)

    process_study.run(str(study_id), str(job_id))

    db = TestingSessionLocal()
    run = db.get(PipelineRun, study_id)
    job = db.get(StepRun, job_id)
    assert run.status == "failed"
    assert job.status == "failed" and job.outcome == "failed"
    assert run.failure_message == "The external processor finished with an error"
    notification = db.query(Notification).one()
    assert notification.recipient_user_id == owner_id
    assert notification.event_type == "processing_failed"
    assert notification.email_status == "disabled"
    assert notification.case_id == study_id and notification.study_id is None
    db.close()


def test_worker_skips_canceled_study(worker_env):
    TestingSessionLocal, storage_root, owner_id = worker_env
    study_id, job_id = seed_legacy_study(
        TestingSessionLocal, storage_root, owner_id, status="canceled"
    )

    process_study.run(str(study_id), str(job_id))

    db = TestingSessionLocal()
    run = db.get(PipelineRun, study_id)
    job = db.get(StepRun, job_id)
    assert run.status == "canceled"
    assert job.status == "canceled"
    assert job.started_at is None
    db.close()


@pytest.mark.parametrize(
    ("flag_set", "error_message"),
    [
        # The Spanish literal is the legacy cancellation message that may still
        # exist in old database rows; it must have no influence on behaviour.
        (True, "Cancelación solicitada por el usuario"),
        (True, "arbitrary text unrelated to cancellation"),
        (False, "Cancelación solicitada por el usuario"),
    ],
)
def test_worker_cancellation_depends_only_on_flag(
    worker_env, monkeypatch, flag_set, error_message
):
    TestingSessionLocal, storage_root, owner_id = worker_env
    study_id, job_id = seed_legacy_study(TestingSessionLocal, storage_root, owner_id)

    import worker.tasks as tasks

    class CancelingAdapter:
        @staticmethod
        def run(**_kwargs):
            db = TestingSessionLocal()
            job = db.get(StepRun, job_id)
            job.failure_message = error_message
            if flag_set:
                job.cancel_requested_at = datetime.now(timezone.utc)
            db.commit()
            db.close()
            raise ProcessorCanceled("Processing canceled by signal 15")

    monkeypatch.setattr(
        tasks, "create_processor_adapter", lambda **_kwargs: CancelingAdapter()
    )

    process_study.run(str(study_id), str(job_id))

    db = TestingSessionLocal()
    run = db.get(PipelineRun, study_id)
    job = db.get(StepRun, job_id)
    if flag_set:
        assert run.status == "canceled"
        assert run.failure_message == "Canceled by the user"
        assert job.status == "canceled"
        assert job.failure_message == "Processing canceled by signal 15"
        assert db.query(Notification).count() == 0
    else:
        # Message text alone must never be treated as a cancellation.
        assert run.status == "failed"
        assert job.status == "failed"
    db.close()
