from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    Query,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import ErrorCode, api_error
from app.db.session import get_db
from app.models.notification import Notification
from app.models.user import User
from app.schemas.auth import (
    NotificationPreferences,
)
from app.schemas.notification import NotificationRead
from app.services.audit import record_event
from app.services.auth import (
    get_current_user,
)

router = APIRouter()


@router.get("/me/notification-preferences", response_model=NotificationPreferences)
def get_notification_preferences(
    current_user: User = Depends(get_current_user),
) -> NotificationPreferences:
    return NotificationPreferences(
        notify_on_processing_completed=current_user.notify_on_processing_completed,
        notify_on_processing_failed=current_user.notify_on_processing_failed,
    )


@router.patch("/me/notification-preferences", response_model=NotificationPreferences)
def update_notification_preferences(
    payload: NotificationPreferences,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationPreferences:
    current_user.notify_on_processing_completed = payload.notify_on_processing_completed
    current_user.notify_on_processing_failed = payload.notify_on_processing_failed
    record_event(
        db,
        "notification_preferences_updated",
        actor=current_user.email,
        actor_user_id=current_user.id,
        details=payload.model_dump(),
    )
    db.commit()
    return payload


@router.get("/notifications", response_model=list[NotificationRead])
def list_notifications(
    limit: int = Query(default=30, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[NotificationRead]:
    notifications = db.scalars(
        select(Notification)
        .where(Notification.recipient_user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .limit(limit)
    ).all()
    return [NotificationRead.model_validate(item) for item in notifications]


@router.post("/notifications/{notification_id}/read", response_model=NotificationRead)
def mark_notification_read(
    notification_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> NotificationRead:
    notification = db.get(Notification, notification_id)
    if not notification or notification.recipient_user_id != current_user.id:
        raise api_error(ErrorCode.NOTIFICATION_NOT_FOUND)
    if not notification.read_at:
        notification.read_at = datetime.utcnow()
        db.commit()
        db.refresh(notification)
    return NotificationRead.model_validate(notification)
