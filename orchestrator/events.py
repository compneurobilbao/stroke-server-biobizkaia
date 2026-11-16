"""Audit events and notifications of pipeline runs (docs/data-model.md section 6).

New event types: ``run_succeeded``, ``run_failed``, ``run_canceled`` (audit), and
the same ``run_succeeded`` / ``run_failed`` as notifications, created through
the existing notification service (``create_notification``: internal notice
plus e-mail when SMTP is configured and the user's preference allows it).
Recipients follow the legacy rules: the case owner on success; the owner and
active admins on failure. Messages carry no file names or identifiers other than
the opaque run id.
"""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent
from app.models.case import Case
from app.models.pipeline_run import PipelineRun
from app.models.user import User, UserRole
from app.services.notifications import create_notification

log = logging.getLogger(__name__)

ORCHESTRATOR_ACTOR = "system/orchestrator"


def audit(
    db: Session,
    event_type: str,
    run: PipelineRun,
    details: dict | None = None,
    *,
    actor: str = ORCHESTRATOR_ACTOR,
    actor_user_id: uuid.UUID | None = None,
) -> None:
    db.add(
        AuditEvent(
            case_id=run.case_id,
            pipeline_run_id=run.id,
            event_type=event_type,
            details=json.dumps(details or {}),
            actor=actor,
            actor_user_id=actor_user_id,
        )
    )


def notify_run_finished(db: Session, run: PipelineRun, settings) -> None:
    """Create notifications for a run that just became ``succeeded`` or ``failed``.

    Called after the run's final state is committed; an SMTP failure is recorded
    on the notification and never changes the run (same rule as legacy studies).
    """
    if run.status not in ("succeeded", "failed"):
        return
    case = db.get(Case, run.case_id)
    owner = db.get(User, case.owner_user_id) if case else None
    recipients: list[User] = []
    if owner is not None and owner.is_active:
        recipients.append(owner)
    if run.status == "failed":
        recipients += db.scalars(
            select(User).where(
                User.role == UserRole.admin.value, User.is_active.is_(True)
            )
        ).all()
    seen: set[uuid.UUID] = set()
    for user in recipients:
        if user.id in seen:
            continue
        seen.add(user.id)
        if run.status == "succeeded":
            title = "Pipeline run completed"
            message = (
                f"Run {run.id} of pipeline {run.pipeline_id} {run.pipeline_version} "
                "finished successfully. Results are available on the platform."
            )
            allowed = user.notify_on_processing_completed
        else:
            title = (
                "Pipeline run error requires review"
                if user.role == UserRole.admin.value
                else "Pipeline run failed"
            )
            message = (
                f"Run {run.id} of pipeline {run.pipeline_id} {run.pipeline_version} "
                f"failed: {run.failure_message or 'no detail recorded'}"
            )
            allowed = user.notify_on_processing_failed
        notification = create_notification(
            db,
            recipient=user,
            study=None,
            event_type=f"run_{run.status}",
            title=title,
            message=message,
            settings=settings,
            email_allowed=allowed,
        )
        notification.case_id = run.case_id
        notification.pipeline_run_id = run.id
