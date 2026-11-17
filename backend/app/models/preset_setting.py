"""`PresetSetting` (docs/data-model.md section 4.12)."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.db.base import Base
from app.models import formats
from app.models._common import tz_datetime, utcnow


class PresetSetting(Base):
    """The deployment's enablement and default per preset (docs/design/run-plans.md 2.4).

    Same shape and rules as `SlotImplementationSetting`: keyed by pipeline id and preset
    id, not by version, so that a setting survives a new preset or pipeline version.
    """

    __tablename__ = "preset_settings"
    __table_args__ = (
        CheckConstraint(
            "NOT is_default OR enabled", name="ck_preset_settings_default_enabled"
        ),
        UniqueConstraint("pipeline_id", "preset_id", name="uq_preset_settings"),
        # At most one default preset per pipeline id.
        Index(
            "uq_preset_settings_default",
            "pipeline_id",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pipeline_id: Mapped[str] = mapped_column(String(64), nullable=False)
    preset_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    updated_at: Mapped[datetime] = mapped_column(
        tz_datetime(), nullable=False, default=utcnow, onupdate=utcnow
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))

    @validates("pipeline_id", "preset_id")
    def _validate_kebab(self, _key: str, value: str) -> str:
        formats.check_format("kebab", value)
        return value
