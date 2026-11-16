from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy.orm import Session

from app.models.audit_event import AuditEvent


def record_event(
    db: Session,
    event_type: str,
    study_id: UUID | None = None,
    details: dict | None = None,
    actor: str = "anonymous/system",
    actor_user_id: UUID | None = None,
    ip_address: str | None = None,
    *,
    case_id: UUID | None = None,
    pipeline_run_id: UUID | None = None,
) -> None:
    """Append an audit event.

    Since the legacy cut-over (migration 0012) events about a study set ``case_id``
    (legacy study id = case id) and, when relevant, ``pipeline_run_id``; the platform
    no longer writes ``study_id`` (docs/data-model.md section 6).
    """
    event = AuditEvent(
        study_id=study_id,
        case_id=case_id,
        pipeline_run_id=pipeline_run_id,
        event_type=event_type,
        details=json.dumps(details or {}, ensure_ascii=False),
        actor=actor,
        actor_user_id=actor_user_id,
        ip_address=ip_address,
    )
    db.add(event)
