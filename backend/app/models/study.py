from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.user import SYSTEM_USER_ID


class StudyStatus(str, enum.Enum):
    uploaded = "uploaded"
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"


class ClinicalReviewStatus(str, enum.Enum):
    technical_only = "technical_only"
    reviewed = "reviewed"
    validated = "validated"


class Study(Base):
    __tablename__ = "studies"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), default=SYSTEM_USER_ID, nullable=False, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    output_path: Mapped[str | None] = mapped_column(Text)
    pdf_path: Mapped[str | None] = mapped_column(Text)
    output_zip_path: Mapped[str | None] = mapped_column(Text)
    bids_subject_id: Mapped[str | None] = mapped_column(String(128))
    processor_backend: Mapped[str | None] = mapped_column(String(100))
    container_image: Mapped[str | None] = mapped_column(String(255))
    bids_path: Mapped[str | None] = mapped_column(Text)
    preproc_output_path: Mapped[str | None] = mapped_column(Text)
    rendered_png_dir: Mapped[str | None] = mapped_column(Text)
    processing_warnings: Mapped[str | None] = mapped_column(Text)
    clinical_review_status: Mapped[str] = mapped_column(
        String(50), default=ClinicalReviewStatus.technical_only.value, nullable=False
    )
    status: Mapped[StudyStatus] = mapped_column(
        Enum(StudyStatus), default=StudyStatus.uploaded, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )
    processing_started_at: Mapped[datetime | None] = mapped_column(DateTime)
    processing_finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime)
    keep_forever: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    processor_name: Mapped[str | None] = mapped_column(String(255))
    processor_version: Mapped[str | None] = mapped_column(String(100))
    file_size: Mapped[int | None] = mapped_column(BigInteger)
    checksum: Mapped[str | None] = mapped_column(String(128))

    jobs = relationship(
        "ProcessingJob", back_populates="study", cascade="all, delete-orphan"
    )
    audit_events = relationship(
        "AuditEvent", back_populates="study", cascade="all, delete-orphan"
    )
    share_links = relationship(
        "ShareLink", back_populates="study", cascade="all, delete-orphan"
    )
    notifications = relationship(
        "Notification", back_populates="study", cascade="all, delete-orphan"
    )
    owner = relationship("User", back_populates="studies")
