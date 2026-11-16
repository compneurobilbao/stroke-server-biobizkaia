"""Recovery of legacy attempts left without a worker task (worker/legacy_recovery.py)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.models import AuditEvent, Case, PipelineRun, StepRun, User
from tests import pgdb
from tests.legacy_state import seed_legacy_study
from worker.legacy_recovery import LOST_CODE, recover_legacy_runs, thresholds

NOW = datetime.now(timezone.utc)
HOUR = timedelta(hours=1)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "studies"
    monkeypatch.setenv("STORAGE_ROOT", str(root))
    engine = pgdb.new_engine(
        tmp_path, "recovery.db", connect_args={"check_same_thread": False}
    )
    monkeypatch.setenv("DATABASE_URL", pgdb.database_url(engine))
    get_settings.cache_clear()
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    owner = uuid4()
    with Session() as db:
        db.add(User(id=owner, email="owner@example.org", full_name="O", role="researcher"))
        db.commit()

    def study(status="queued", *, queued_ago=2 * HOUR, started_ago=None, task="t-0"):
        case_id, step_id = seed_legacy_study(Session, root, owner, status=status)
        with Session() as db:
            step = db.get(StepRun, step_id)
            step.queued_at = NOW - queued_ago
            step.orchestrator_task_id = task
            if started_ago is not None:
                step.started_at = NOW - started_ago
                db.get(PipelineRun, case_id).started_at = NOW - started_ago
            db.commit()
        return case_id, step_id

    yield Session, study
    get_settings.cache_clear()


def run_recovery(Session, *, live, execute=True, failed=None):
    sent = []
    report = recover_legacy_runs(
        Session,
        live_ids=live,
        enqueue=lambda case_id, step_id, task_id: sent.append((case_id, step_id, task_id)),
        queued_after=timedelta(minutes=30),
        running_after=6 * HOUR,
        execute=execute,
        now=NOW,
        on_failed=(lambda db, case, run: failed.append(run.id)) if failed is not None else None,
    )
    return report, sent


def test_orphaned_queued_attempt_is_requeued_once(env):
    Session, study = env
    orphan_case, orphan = study("queued", task="t-lost")
    _fresh_case, fresh = study("queued", queued_ago=timedelta(minutes=5))
    _live_case, live = study("queued", task="t-live")
    deleted_case, deleted = study("queued")
    with Session() as db:
        db.get(Case, deleted_case).deleted_at = NOW
        db.commit()

    report, sent = run_recovery(Session, live={"t-live"}, execute=False)
    assert report.requeued == [str(orphan)] and sent == []
    with Session() as db:  # a dry run writes nothing
        assert db.get(StepRun, orphan).orchestrator_task_id == "t-lost"

    report, sent = run_recovery(Session, live={"t-live"})
    assert report.requeued == [str(orphan)]
    ((case_id, step_id, task_id),) = sent
    assert (case_id, step_id) == (orphan_case, orphan) and task_id != "t-lost"
    with Session() as db:
        step = db.get(StepRun, orphan)
        assert step.status == "queued" and step.orchestrator_task_id == task_id
        assert step.executor_state["requeued_at"]
        event = db.scalar(
            select(AuditEvent).where(AuditEvent.event_type == "processing_requeued")
        )
        assert event.case_id == orphan_case and event.actor == "system/legacy-recovery"
        assert json.loads(event.details)["previous_task_id"] == "t-lost"
        for other in (fresh, live, deleted):
            assert db.get(StepRun, other).executor_state is None

    # Idempotent: a second pass (another replica, or beat) sends nothing.
    report, sent = run_recovery(Session, live={"t-live"})
    assert sent == [] and report.already_requeued == [str(orphan)]


def test_requeued_attempt_runs_once_and_records_the_running_task(env, monkeypatch):
    """The recovery's message and a late original one: the first starts the attempt and
    records its task id (the one a cancel revokes); the second skips it."""
    import worker.tasks as tasks

    Session, study = env
    monkeypatch.setattr(tasks, "SessionLocal", Session)

    class Boom:
        @staticmethod
        def run(**_kwargs):
            raise RuntimeError("synthetic processor failure")

    monkeypatch.setattr(tasks, "create_processor_adapter", lambda **_k: Boom())
    case_id, step_id = study("queued", task="t-lost")
    _report, sent = run_recovery(Session, live=set())
    ((_c, _s, task_id),) = sent

    tasks.process_study.apply(args=(str(case_id), str(step_id)), task_id=task_id)
    tasks.process_study.apply(args=(str(case_id), str(step_id)), task_id="t-lost")
    with Session() as db:
        step = db.get(StepRun, step_id)
        assert step.orchestrator_task_id == task_id
        assert step.status == "failed"  # ran once, by the recovery's message
        assert db.get(PipelineRun, case_id).failure_message == (
            "Internal error during processing"
        )


def test_running_attempt_without_task_is_failed_only_when_known_lost(env):
    Session, study = env
    lost_case, lost = study("processing", started_ago=10 * HOUR, task="t-dead")
    _alive_case, alive = study("processing", started_ago=10 * HOUR, task="t-alive")
    _young_case, young = study("processing", started_ago=HOUR, task="t-young")

    # No worker answered: nothing is known about running tasks; nothing is failed.
    report, _ = run_recovery(Session, live=None)
    assert report.failed_lost == [] and report.workers_answered is False

    failed = []
    report, _ = run_recovery(Session, live={"t-alive"}, failed=failed)
    assert report.failed_lost == [str(lost)]
    assert failed == [lost_case]
    with Session() as db:
        step = db.get(StepRun, lost)
        run = db.get(PipelineRun, lost_case)
        assert (step.status, step.outcome, step.failure_code) == (
            "failed",
            "failed",
            LOST_CODE,
        )
        assert (run.status, run.failure_code) == ("failed", LOST_CODE)
        assert run.finished_at is not None
        assert db.get(StepRun, alive).status == "running"
        assert db.get(StepRun, young).status == "running"


def test_thresholds_respect_the_processing_timeout():
    class S:
        legacy_recovery_queued_minutes = 30
        legacy_recovery_running_hours = 6
        processing_timeout_seconds = 0

    assert thresholds(S) == (timedelta(minutes=30), timedelta(hours=6))
    S.processing_timeout_seconds = 12 * 3600
    assert thresholds(S)[1] == timedelta(hours=12, minutes=30)
