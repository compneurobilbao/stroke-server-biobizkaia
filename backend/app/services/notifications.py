from __future__ import annotations

import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.notification import Notification
from app.models.study import Study, StudyStatus
from app.models.user import User, UserRole


def notify_processing_finished(
    db: Session,
    study: Study,
    settings: Settings,
) -> list[Notification]:
    if study.status == StudyStatus.completed:
        return _notify_completed(db, study, settings)
    if study.status == StudyStatus.failed:
        return _notify_failed(db, study, settings)
    return []


def _notify_completed(
    db: Session, study: Study, settings: Settings
) -> list[Notification]:
    owner = study.owner
    if not owner or not owner.is_active:
        return []
    return [
        create_notification(
            db,
            recipient=owner,
            study=study,
            event_type="processing_completed",
            title="Processing completed",
            message=(
                f"Study {study.original_filename} finished successfully. "
                "You can now review and download the technical report from the platform."
            ),
            settings=settings,
            email_allowed=owner.notify_on_processing_completed,
        )
    ]


def _notify_failed(db: Session, study: Study, settings: Settings) -> list[Notification]:
    recipients = []
    if study.owner and study.owner.is_active:
        recipients.append(study.owner)
    admins = db.scalars(
        select(User).where(User.role == UserRole.admin.value, User.is_active.is_(True))
    ).all()
    recipients.extend(admins)

    notifications = []
    seen = set()
    for recipient in recipients:
        if recipient.id in seen:
            continue
        seen.add(recipient.id)
        title = "Processing failed"
        if recipient.role == UserRole.admin.value:
            title = "Processing error requires review"
        notifications.append(
            create_notification(
                db,
                recipient=recipient,
                study=study,
                event_type="processing_failed",
                title=title,
                message=(
                    f"Study {study.original_filename} finished with an error. "
                    f"Recorded detail: {study.error_message or 'no technical detail'}."
                ),
                settings=settings,
                email_allowed=recipient.notify_on_processing_failed,
            )
        )
    return notifications


def create_notification(
    db: Session,
    recipient: User,
    study: Study | None,
    event_type: str,
    title: str,
    message: str,
    settings: Settings,
    email_allowed: bool = True,
) -> Notification:
    notification = Notification(
        recipient_user_id=recipient.id,
        study_id=study.id if study else None,
        event_type=event_type,
        title=title,
        message=message,
        email_status="pending"
        if _smtp_ready(settings) and email_allowed
        else "disabled",
    )
    db.add(notification)
    if notification.email_status != "pending":
        return notification
    try:
        _send_email(
            settings=settings,
            to_email=recipient.email,
            subject=title,
            body=_email_body(message, study, settings),
        )
    except Exception as exc:
        notification.email_status = "failed"
        notification.email_error = str(exc)[:2000]
    else:
        notification.email_status = "sent"
        notification.email_sent_at = datetime.utcnow()
    return notification


def _smtp_ready(settings: Settings) -> bool:
    return bool(
        settings.notifications_email_enabled
        and settings.smtp_host
        and settings.smtp_from_email
    )


def _send_email(
    settings: Settings,
    to_email: str,
    subject: str,
    body: str,
) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from_email or ""
    message["To"] = to_email
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host or "", settings.smtp_port, timeout=10) as smtp:
        if settings.smtp_use_tls:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if settings.smtp_username and settings.smtp_password:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


def _email_body(message: str, study: Study | None, settings: Settings) -> str:
    lines = [message]
    if study:
        base_url = settings.app_public_base_url.rstrip("/")
        lines.extend(
            [
                "",
                f"Study: {study.id}",
                f"File: {study.original_filename}",
                f"Platform access: {base_url}",
                "",
                "No PDF, ZIP or other large data are attached to this email.",
            ]
        )
    return "\n".join(lines)
