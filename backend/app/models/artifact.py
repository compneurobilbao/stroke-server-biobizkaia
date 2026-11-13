"""`Artifact` and `Report` (docs/data-model.md sections 4.8 and 4.10)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.base import Base
from app.models import formats
from app.models._common import enum_check, tz_datetime, utcnow
from app.models.case import check_relative_path

LEGACY_REPORT_TEMPLATE = "legacy-technical"


class ArtifactOrigin(str, enum.Enum):
    step = "step"
    review = "review"
    legacy = "legacy"


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (
        enum_check("artifacts", "origin", [e.value for e in ArtifactOrigin]),
        CheckConstraint(
            "origin = 'legacy' OR sha256 IS NOT NULL", name="ck_artifacts_sha256_required"
        ),
        CheckConstraint(
            "origin = 'legacy' OR size_bytes IS NOT NULL", name="ck_artifacts_size_required"
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name="ck_artifacts_size_bytes"
        ),
        CheckConstraint(
            "origin = 'review' OR step_run_id IS NOT NULL",
            name="ck_artifacts_step_run_required",
        ),
        Index(
            "uq_artifacts_step_port",
            "step_run_id",
            "port",
            unique=True,
            postgresql_where=text("origin <> 'review'"),
            sqlite_where=text("origin <> 'review'"),
        ),
        Index("ix_artifacts_case_id", "case_id"),
        Index("ix_artifacts_run_type", "pipeline_run_id", "artefact_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), nullable=False)
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False
    )
    step_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("step_runs.id", ondelete="CASCADE")
    )
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    port: Mapped[str] = mapped_column(String(64), nullable=False)
    artefact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    supersedes_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifacts.id")
    )
    created_at: Mapped[datetime] = mapped_column(
        tz_datetime(), nullable=False, default=utcnow
    )

    step_run = relationship("StepRun")

    @validates("port", "artefact_type")
    def _validate_snake(self, _key: str, value: str) -> str:
        formats.check_format("snake", value)
        return value

    @validates("sha256")
    def _validate_sha(self, _key: str, value: str | None) -> str | None:
        return formats.check_format("sha256hex", value)

    @validates("storage_path")
    def _validate_storage_path(self, _key: str, value: str) -> str:
        return check_relative_path(value)


class Report(Base):
    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint(
            f"template = '{LEGACY_REPORT_TEMPLATE}' OR json_artifact_id IS NOT NULL",
            name="ck_reports_json_artifact_required",
        ),
        CheckConstraint(
            f"template = '{LEGACY_REPORT_TEMPLATE}' OR content_sha256 IS NOT NULL",
            name="ck_reports_content_sha256_required",
        ),
        UniqueConstraint(
            "pipeline_run_id",
            "template",
            "language",
            name="uq_reports_run_template_language",
        ),
        Index("ix_reports_case_id", "case_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), nullable=False)
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.id"), nullable=False
    )
    step_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("step_runs.id"), nullable=False
    )
    pdf_artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id"), nullable=False
    )
    json_artifact_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("artifacts.id")
    )
    template: Mapped[str] = mapped_column(String(64), nullable=False)
    language: Mapped[str] = mapped_column(String(8), nullable=False)
    content_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        tz_datetime(), nullable=False, default=utcnow
    )

    pdf_artifact = relationship("Artifact", foreign_keys=[pdf_artifact_id])
    json_artifact = relationship("Artifact", foreign_keys=[json_artifact_id])

    @validates("template")
    def _validate_template(self, _key: str, value: str) -> str:
        formats.check_format("kebab", value)
        return value

    @validates("content_sha256")
    def _validate_sha(self, _key: str, value: str | None) -> str | None:
        return formats.check_format("sha256hex", value)
