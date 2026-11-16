from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import redis
from fastapi import (
    APIRouter,
    Depends,
)
from sqlalchemy import and_, func, select, text
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.models.case import Case
from app.models.pipeline_run import LEGACY_PIPELINE_ID, PipelineRun, StepRun
from app.models.user import User, UserRole
from app.schemas.admin import (
    AdminDashboardRead,
    AdminQueueSummary,
    AdminServiceHealth,
    AdminStorageSummary,
    AdminUserSummary,
)
from app.services import legacy_studies as legacy
from app.services.auth import (
    require_admin,
)
from app.services.quota import storage_used
from app.services.storage import LocalStudyStorage

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/admin/dashboard", response_model=AdminDashboardRead)
def admin_dashboard(
    _: User = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> AdminDashboardRead:
    # Legacy view over the run tables (docs/data-model.md 10.4): a study is a case of the
    # legacy profile, its status is its latest run's, mapped; a job is an attempt of a
    # legacy run. Cases and runs of /api/v1 are not counted here.
    studies_by_status = _studies_by_status(db)
    jobs_by_status = _jobs_by_status(db)
    worker_stats, worker_error = _inspect_worker_stats()
    worker_capacity = _worker_capacity(settings, worker_stats)
    queue = AdminQueueSummary(
        queued=jobs_by_status.get("queued", 0),
        processing=jobs_by_status.get("processing", 0),
        failed=jobs_by_status.get("failed", 0),
        worker_replicas=worker_capacity["worker_replicas"],
        worker_concurrency=worker_capacity["worker_concurrency"],
        processing_capacity=worker_capacity["processing_capacity"],
    )
    queue.active = queue.queued + queue.processing
    queue.processing_available = max(0, queue.processing_capacity - queue.processing)
    users = _user_summary(db)
    storage = _storage_summary(settings.storage_root, storage_used(db, None))
    services = _service_health(db, settings, worker_stats, worker_error)
    case_store = LocalStudyStorage(settings.storage_root)
    recent_failed_jobs = [
        legacy.job_read(step, case_id, case_store)
        for step, case_id in db.execute(
            select(StepRun, PipelineRun.case_id)
            .join(PipelineRun, PipelineRun.id == StepRun.pipeline_run_id)
            .where(
                StepRun.status == "failed",
                PipelineRun.pipeline_id == LEGACY_PIPELINE_ID,
                # attempts closed because a newer one replaced them are not failures
                StepRun.failure_code.is_distinct_from(legacy.STALE_ATTEMPT_CODE),
            )
            .order_by(StepRun.queued_at.desc())
            .limit(5)
        ).all()
    ]
    alerts = _dashboard_alerts(queue, storage, services)
    return AdminDashboardRead(
        generated_at=datetime.utcnow(),
        queue=queue,
        users=users,
        studies_by_status=studies_by_status,
        jobs_by_status=jobs_by_status,
        storage=storage,
        services=services,
        recent_failed_jobs=recent_failed_jobs,
        alerts=alerts,
    )


def _studies_by_status(db: Session) -> dict[str, int]:
    """Non-deleted legacy cases by the mapped status of their latest run."""
    ranked = select(
        PipelineRun.case_id,
        PipelineRun.status,
        func.row_number()
        .over(
            partition_by=PipelineRun.case_id,
            order_by=(PipelineRun.created_at.desc(), PipelineRun.id.desc()),
        )
        .label("rn"),
    ).subquery()
    rows = db.execute(
        select(ranked.c.status, func.count())
        .select_from(Case)
        .outerjoin(ranked, and_(ranked.c.case_id == Case.id, ranked.c.rn == 1))
        .where(
            Case.deleted_at.is_(None),
            Case.deployment_profile == legacy.LEGACY_PROFILE,
        )
        .group_by(ranked.c.status)
    ).all()
    counts: dict[str, int] = {}
    for status, count in rows:
        key = legacy.STUDY_STATUS[status] if status else legacy.NO_RUN_STATUS
        counts[key] = counts.get(key, 0) + count
    return counts


def _jobs_by_status(db: Session) -> dict[str, int]:
    counts: dict[str, int] = {}
    for status, count in db.execute(
        select(StepRun.status, func.count())
        .join(PipelineRun, PipelineRun.id == StepRun.pipeline_run_id)
        .where(PipelineRun.pipeline_id == LEGACY_PIPELINE_ID)
        .group_by(StepRun.status)
    ).all():
        key = legacy.STUDY_STATUS[status]
        counts[key] = counts.get(key, 0) + count
    return counts


def _user_summary(db: Session) -> AdminUserSummary:
    visible_users = User.deleted_at.is_(None)
    total = db.scalar(select(func.count()).select_from(User).where(visible_users)) or 0
    active = (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(visible_users, User.is_active.is_(True))
        )
        or 0
    )
    admins = (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(visible_users, User.role == UserRole.admin.value)
        )
        or 0
    )
    researchers = (
        db.scalar(
            select(func.count())
            .select_from(User)
            .where(visible_users, User.role == UserRole.researcher.value)
        )
        or 0
    )
    return AdminUserSummary(
        total=total, active=active, admins=admins, researchers=researchers
    )


def _storage_summary(root: Path, studies_bytes: int) -> AdminStorageSummary:
    root = Path(root)
    exists = root.exists()
    disk_path = root if exists else root.parent
    while not disk_path.exists() and disk_path != disk_path.parent:
        disk_path = disk_path.parent
    try:
        usage = shutil.disk_usage(disk_path)
    except OSError:
        disk_total = disk_used = disk_free = 0
    else:
        disk_total = usage.total
        disk_used = usage.used
        disk_free = usage.free
    return AdminStorageSummary(
        root=str(root),
        exists=exists,
        studies_bytes=studies_bytes,
        disk_total_bytes=disk_total,
        disk_used_bytes=disk_used,
        disk_free_bytes=disk_free,
    )


def _service_health(
    db: Session,
    settings: Settings,
    worker_stats: dict[str, Any] | None,
    worker_error: str | None,
) -> list[AdminServiceHealth]:
    return [
        _database_health(db),
        _redis_health(settings),
        _worker_health(worker_stats, worker_error),
    ]


def _database_health(db: Session) -> AdminServiceHealth:
    try:
        db.execute(text("SELECT 1"))
    except Exception as exc:
        return AdminServiceHealth(name="PostgreSQL", status="down", detail=str(exc))
    return AdminServiceHealth(name="PostgreSQL", status="ok")


def _redis_health(settings: Settings) -> AdminServiceHealth:
    broker_url = settings.celery_broker_url
    if not broker_url.startswith(("redis://", "rediss://")):
        return AdminServiceHealth(
            name="Redis", status="unknown", detail="Broker is not Redis in this environment"
        )
    try:
        client = redis.Redis.from_url(
            broker_url, socket_connect_timeout=0.5, socket_timeout=0.5
        )
        client.ping()
    except Exception as exc:
        return AdminServiceHealth(name="Redis", status="down", detail=str(exc))
    return AdminServiceHealth(name="Redis", status="ok")


def _inspect_worker_stats() -> tuple[dict[str, Any] | None, str | None]:
    try:
        from worker.celery_app import celery_app

        return celery_app.control.inspect(timeout=2.0).stats() or {}, None
    except Exception as exc:
        return None, str(exc)


def _worker_health(
    worker_stats: dict[str, Any] | None,
    worker_error: str | None,
) -> AdminServiceHealth:
    if worker_error:
        return AdminServiceHealth(name="Worker", status="down", detail=worker_error)
    if not worker_stats:
        return AdminServiceHealth(
            name="Worker", status="warning", detail="No response from Celery workers"
        )
    return AdminServiceHealth(
        name="Worker", status="ok", detail=f"{len(worker_stats)} worker(s)"
    )


def _worker_capacity(
    settings: Settings,
    worker_stats: dict[str, Any] | None,
) -> dict[str, int]:
    fallback_concurrency = max(1, settings.max_concurrent_processing_jobs)
    if not worker_stats:
        return {
            "worker_replicas": 0,
            "worker_concurrency": fallback_concurrency,
            "processing_capacity": 0,
        }

    worker_replicas = len(worker_stats)
    capacities = []
    for stats in worker_stats.values():
        pool = stats.get("pool") or {}
        raw_capacity = pool.get("max-concurrency") or pool.get("max_concurrency")
        try:
            capacities.append(max(1, int(raw_capacity)))
        except (TypeError, ValueError):
            capacities.append(fallback_concurrency)

    return {
        "worker_replicas": worker_replicas,
        "worker_concurrency": max(capacities) if capacities else fallback_concurrency,
        "processing_capacity": sum(capacities),
    }


def _dashboard_alerts(
    queue: AdminQueueSummary,
    storage: AdminStorageSummary,
    services: list[AdminServiceHealth],
) -> list[str]:
    alerts = []
    if queue.failed:
        alerts.append(f"{queue.failed} failed job(s) require review")
    if queue.queued:
        alerts.append(f"{queue.queued} job(s) waiting in the queue")
    if (
        storage.disk_total_bytes
        and storage.disk_free_bytes / storage.disk_total_bytes < 0.1
    ):
        alerts.append("Less than 10% of disk space is free")
    for service in services:
        if service.status in {"down", "warning"}:
            alerts.append(f"{service.name}: {service.detail or service.status}")
    return alerts
