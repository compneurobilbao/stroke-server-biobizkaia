from __future__ import annotations

import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.models.case import Case
from app.models.notification import Notification
from app.models.pipeline_run import PipelineRun
from app.models.study import Study
from app.models.user import User, UserRole


def notify_processing_finished(
    db: Session,
    case: Case,
    run: PipelineRun,
    settings: Settings,
) -> list[Notification]:
    """Notifications of a finished legacy run (legacy study id = case id).

    Same recipients, titles and messages as before the legacy cut-over; the rows link
    the case and the run instead of the frozen ``studies`` row.
    """
    if run.status == "succeeded":
        return _notify_completed(db, case, run, settings)
    if run.status == "failed":
        return _notify_failed(db, case, run, settings)
    return []


def _display_name(case: Case) -> str | None:
    return (case.legacy_attributes or {}).get("original_filename")


def _notify_completed(
    db: Session, case: Case, run: PipelineRun, settings: Settings
) -> list[Notification]:
    owner = db.get(User, case.owner_user_id)
    if not owner or not owner.is_active:
        return []
    name = _display_name(case)
    return [
        create_notification(
            db,
            recipient=owner,
            study=None,
            event_type="processing_completed",
            title="Processing completed",
            message=(
                f"Study {name} finished successfully. "
                "You can now review and download the technical report from the platform."
            ),
            settings=settings,
            email_allowed=owner.notify_on_processing_completed,
            case_id=case.id,
            pipeline_run_id=run.id,
            email_reference=(case.id, name),
        )
    ]


def _notify_failed(
    db: Session, case: Case, run: PipelineRun, settings: Settings
) -> list[Notification]:
    recipients = []
    owner = db.get(User, case.owner_user_id)
    if owner and owner.is_active:
        recipients.append(owner)
    admins = db.scalars(
        select(User).where(User.role == UserRole.admin.value, User.is_active.is_(True))
    ).all()
    recipients.extend(admins)
    name = _display_name(case)

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
                study=None,
                event_type="processing_failed",
                title=title,
                message=(
                    f"Study {name} finished with an error. "
                    f"Recorded detail: {run.failure_message or 'no technical detail'}."
                ),
                settings=settings,
                email_allowed=recipient.notify_on_processing_failed,
                case_id=case.id,
                pipeline_run_id=run.id,
                email_reference=(case.id, name),
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
    *,
    case_id: UUID | None = None,
    pipeline_run_id: UUID | None = None,
    email_reference: tuple[UUID, str | None] | None = None,
) -> Notification:
    """Store a notification and send its e-mail when SMTP and the preference allow it.

    ``email_reference`` (study id and file name) adds the legacy reference lines to the
    e-mail body; ``study`` is kept for callers that still pass a ``Study``.
    """
    if study is not None and email_reference is None:
        email_reference = (study.id, study.original_filename)
    notification = Notification(
        recipient_user_id=recipient.id,
        study_id=study.id if study else None,
        case_id=case_id,
        pipeline_run_id=pipeline_run_id,
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
            body=_email_body(message, email_reference, settings),
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


def _email_body(
    message: str, reference: tuple[UUID, str | None] | None, settings: Settings
) -> str:
    lines = [message]
    if reference:
        study_id, filename = reference
        base_url = settings.app_public_base_url.rstrip("/")
        lines.extend(
            [
                "",
                f"Study: {study_id}",
                f"File: {filename}",
                f"Platform access: {base_url}",
                "",
                "No PDF, ZIP or other large data are attached to this email.",
            ]
        )
    return "\n".join(lines)
