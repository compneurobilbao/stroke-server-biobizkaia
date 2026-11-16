"""Recovery of legacy attempts left without a live worker task.

Two situations leave a legacy run without anybody to drive it:

- **Orphaned queued attempt.** Its ``process_study`` message was lost: queued before a
  deploy and consumed by a worker of the previous release that then stopped, acked by
  a worker that crashed, or dropped by the broker. The run stays ``queued`` forever.
  Recovery re-enqueues the attempt once: it claims the attempt with a compare-and-set
  on ``step_runs.orchestrator_task_id`` (old value -> a new task id) and marks it in
  ``executor_state.requeued_at``, then sends ``process_study`` with that task id. Two
  recoveries running at the same time cannot both claim it. If the original message
  was only waiting in the broker, both messages exist; the task starts the attempt
  with a compare-and-set and records its own task id, so the second message skips it
  and a cancellation always reaches the task that runs.
- **Stuck running attempt** (worker killed while processing). The attempt is
  ``running`` but its task is not active on any worker. Recovery marks it and its run
  ``failed`` with ``failure_code = 'legacy_worker_lost'`` (the user can retry it), but
  only when the workers answered the inspection (so "not active" is known) and the
  attempt started longer ago than the processing timeout allows.

It runs from ``python -m app.cli.legacy_recover`` (dry run by default), from the
``recover_legacy_runs`` task that every legacy worker schedules at start-up, and from
celery beat where a beat process runs. Every action is logged and audited.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent
from app.models.case import Case
from app.models.pipeline_run import LEGACY_PIPELINE_ID, PipelineRun, StepRun

log = logging.getLogger(__name__)

ACTOR = "system/legacy-recovery"
LOST_CODE = "legacy_worker_lost"
LOST_MESSAGE = "Processing was interrupted because the worker stopped"
RUNNING_GRACE = timedelta(minutes=30)


@dataclass
class RecoveryReport:
    dry_run: bool
    workers_answered: bool
    requeued: list[str] = field(default_factory=list)
    already_requeued: list[str] = field(default_factory=list)
    failed_lost: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "dry_run": self.dry_run,
            "workers_answered": self.workers_answered,
            "requeued": self.requeued,
            "already_requeued": self.already_requeued,
            "failed_lost": self.failed_lost,
        }


def live_task_ids(app, timeout: float = 2.0) -> set[str] | None:
    """Ids of tasks active, reserved or scheduled on any worker; None if no worker
    answered (then nothing is known about running tasks)."""
    try:
        inspector = app.control.inspect(timeout=timeout)
        replies = [inspector.active(), inspector.reserved(), inspector.scheduled()]
    except Exception:
        log.exception("Could not inspect the Celery workers")
        return None
    if all(reply is None for reply in replies):
        return None
    ids: set[str] = set()
    for reply in replies:
        for tasks in (reply or {}).values():
            for task in tasks or []:
                info = task.get("request", task) if isinstance(task, dict) else {}
                if info.get("id"):
                    ids.add(info["id"])
    return ids


def _audit(db: Session, event_type: str, run: PipelineRun, details: dict) -> None:
    db.add(
        AuditEvent(
            case_id=run.case_id,
            pipeline_run_id=run.id,
            event_type=event_type,
            details=json.dumps(details),
            actor=ACTOR,
        )
    )


def _same_task(column, value):
    return column.is_(None) if value is None else column == value


def recover_legacy_runs(
    session_factory: Callable[[], Session],
    *,
    live_ids: set[str] | None,
    enqueue: Callable[[uuid.UUID, uuid.UUID, str], None],
    queued_after: timedelta,
    running_after: timedelta,
    execute: bool,
    now: datetime | None = None,
    on_failed: Callable[[Session, Case, PipelineRun], None] | None = None,
) -> RecoveryReport:
    """One recovery pass. ``enqueue(case_id, step_id, task_id)`` sends the task."""
    now = now or datetime.now(timezone.utc)
    report = RecoveryReport(dry_run=not execute, workers_answered=live_ids is not None)
    live = live_ids or set()
    with session_factory() as db:
        queued = db.execute(
            select(StepRun, PipelineRun)
            .join(PipelineRun, PipelineRun.id == StepRun.pipeline_run_id)
            .join(Case, Case.id == PipelineRun.case_id)
            .where(
                PipelineRun.pipeline_id == LEGACY_PIPELINE_ID,
                PipelineRun.status == "queued",
                StepRun.status == "queued",
                StepRun.queued_at <= now - queued_after,
                Case.deleted_at.is_(None),
            )
            .order_by(StepRun.queued_at)
        ).all()
        for step, run in queued:
            if step.orchestrator_task_id and step.orchestrator_task_id in live:
                continue
            state = step.executor_state if isinstance(step.executor_state, dict) else {}
            if state.get("requeued_at"):
                report.already_requeued.append(str(step.id))
                continue
            report.requeued.append(str(step.id))
            if not execute:
                continue
            task_id = str(uuid.uuid4())
            claimed = db.execute(
                update(StepRun)
                .where(
                    StepRun.id == step.id,
                    StepRun.status == "queued",
                    _same_task(StepRun.orchestrator_task_id, step.orchestrator_task_id),
                )
                .values(
                    orchestrator_task_id=task_id,
                    executor_state={**state, "requeued_at": now.isoformat()},
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if claimed != 1:
                db.rollback()
                report.requeued.remove(str(step.id))
                continue
            _audit(
                db,
                "processing_requeued",
                run,
                {"job_id": str(step.id), "previous_task_id": step.orchestrator_task_id},
            )
            db.commit()
            log.warning("Re-enqueued orphaned legacy attempt %s of %s", step.id, run.id)
            enqueue(run.case_id, step.id, task_id)

        if live_ids is None:
            return report  # nothing is known about running tasks: never fail them
        running = db.execute(
            select(StepRun, PipelineRun)
            .join(PipelineRun, PipelineRun.id == StepRun.pipeline_run_id)
            .where(
                PipelineRun.pipeline_id == LEGACY_PIPELINE_ID,
                PipelineRun.status == "running",
                StepRun.status == "running",
                StepRun.started_at <= now - running_after,
            )
            .order_by(StepRun.started_at)
        ).all()
        for step, run in running:
            if step.orchestrator_task_id and step.orchestrator_task_id in live:
                continue
            report.failed_lost.append(str(step.id))
            if not execute:
                continue
            step_done = db.execute(
                update(StepRun)
                .where(StepRun.id == step.id, StepRun.status == "running")
                .values(
                    status="failed",
                    outcome="failed",
                    finished_at=now,
                    failure_code=LOST_CODE,
                    failure_message=LOST_MESSAGE,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            run_done = db.execute(
                update(PipelineRun)
                .where(PipelineRun.id == run.id, PipelineRun.status == "running")
                .values(
                    status="failed",
                    finished_at=now,
                    updated_at=now,
                    failure_code=LOST_CODE,
                    failure_message=LOST_MESSAGE,
                )
                .execution_options(synchronize_session=False)
            ).rowcount
            if step_done != 1 or run_done != 1:
                db.rollback()
                report.failed_lost.remove(str(step.id))
                continue
            _audit(
                db,
                "processing_failed",
                run,
                {"error": LOST_MESSAGE, "job_id": str(step.id), "reason": LOST_CODE},
            )
            db.commit()
            log.warning("Legacy attempt %s of %s lost its worker; failed", step.id, run.id)
            if on_failed is not None:
                db.refresh(run)
                on_failed(db, db.get(Case, run.case_id), run)
    return report


def thresholds(settings) -> tuple[timedelta, timedelta]:
    """(queued_after, running_after). A running attempt is only declared lost after the
    processing timeout (when one is set) plus a grace period."""
    queued_after = timedelta(minutes=max(1, settings.legacy_recovery_queued_minutes))
    running_after = timedelta(hours=max(1, settings.legacy_recovery_running_hours))
    if settings.processing_timeout_seconds > 0:
        running_after = max(
            running_after,
            timedelta(seconds=settings.processing_timeout_seconds) + RUNNING_GRACE,
        )
    return queued_after, running_after
