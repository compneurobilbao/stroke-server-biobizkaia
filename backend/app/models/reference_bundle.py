"""`ReferenceBundle` and `SlotImplementationSetting` (docs/data-model.md 4.9 and 4.11)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base
from app.models import formats
from app.models._common import JSONType, enum_check, tz_datetime, utcnow


class BundleStatus(str, enum.Enum):
    active = "active"
    withdrawn = "withdrawn"


class BundleVerificationStatus(str, enum.Enum):
    verified = "verified"
    failed = "failed"


class ReferenceBundle(Base):
    __tablename__ = "reference_bundles"
    __table_args__ = (
        enum_check("reference_bundles", "status", [e.value for e in BundleStatus]),
        enum_check(
            "reference_bundles",
            "verification_status",
            [e.value for e in BundleVerificationStatus],
        ),
        UniqueConstraint("bundle_id", "version", name="uq_reference_bundles_id_version"),
        Index("ix_reference_bundles_type", "bundle_type", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    bundle_id: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False)
    licence_spdx: Mapped[str] = mapped_column(String(255), nullable=False)
    redistributable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    citation: Mapped[str | None] = mapped_column(Text)
    # Copy of bundle.yaml `metadata`. The attribute cannot be called `metadata`
    # (reserved by the declarative base).
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONType, nullable=False, default=dict
    )
    # Relative to the configured bundle root (normally <bundle_id>/<version>).
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=BundleStatus.active.value
    )
    verification_status: Mapped[str] = mapped_column(
        String(16), nullable=False, default=BundleVerificationStatus.verified.value
    )
    registered_at: Mapped[datetime] = mapped_column(
        tz_datetime(), nullable=False, default=utcnow
    )
    registered_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id")
    )
    last_full_verification_at: Mapped[datetime] = mapped_column(
        tz_datetime(), nullable=False, default=utcnow
    )
    withdrawn_at: Mapped[datetime | None] = mapped_column(tz_datetime())

    @validates("bundle_id", "bundle_type")
    def _validate_kebab(self, _key: str, value: str) -> str:
        formats.check_format("kebab", value)
        return value

    @validates("version")
    def _validate_semver(self, _key: str, value: str) -> str:
        formats.check_format("semver", value)
        return value

    @validates("content_digest")
    def _validate_digest(self, _key: str, value: str) -> str:
        formats.check_format("digest", value)
        return value

    @validates("manifest_sha256")
    def _validate_sha(self, _key: str, value: str) -> str:
        formats.check_format("sha256hex", value)
        return value


class SlotImplementationSetting(Base):
    __tablename__ = "slot_implementation_settings"
    __table_args__ = (
        CheckConstraint(
            "NOT is_default OR enabled", name="ck_slot_impl_settings_default_enabled"
        ),
        UniqueConstraint(
            "pipeline_id", "slot", "implementation_id", name="uq_slot_impl_settings"
        ),
        Index(
            "uq_slot_impl_settings_default",
            "pipeline_id",
            "slot",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pipeline_id: Mapped[str] = mapped_column(String(64), nullable=False)
    slot: Mapped[str] = mapped_column(String(64), nullable=False)
    implementation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    updated_at: Mapped[datetime] = mapped_column(
        tz_datetime(), nullable=False, default=utcnow, onupdate=utcnow
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

    @validates("pipeline_id", "slot", "implementation_id")
    def _validate_kebab(self, _key: str, value: str) -> str:
        formats.check_format("kebab", value)
        return value
