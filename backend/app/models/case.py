"""`Case` and `InputFile` (docs/data-model.md sections 4.1 and 4.2)."""

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
from app.models._common import JSONType, enum_check, tz_datetime, utcnow

# Age is stored only as a band string. Owner decision (docs/data-model.md section 12.1):
# 5-year bands 0-4 ... 85-89, then one top band 90-104. The server never receives an
# exact age. Changing these constants needs an ADR.
AGE_BAND_WIDTH_YEARS = 5
AGE_BAND_TOP_LOWER = 90  # lower bound of the collapsed top band
AGE_BAND_TOP_UPPER = 104  # upper bound of the collapsed top band


def valid_age_bands() -> list[str]:
    bands = [
        f"{lo}-{lo + AGE_BAND_WIDTH_YEARS - 1}"
        for lo in range(0, AGE_BAND_TOP_LOWER, AGE_BAND_WIDTH_YEARS)
    ]
    bands.append(f"{AGE_BAND_TOP_LOWER}-{AGE_BAND_TOP_UPPER}")
    return bands


def is_valid_age_band(value: str) -> bool:
    return value in _AGE_BANDS


def age_band_midpoint(value: str) -> float:
    """Midpoint of a valid band, used by the template binding of the normalisation step."""
    if not is_valid_age_band(value):
        raise ValueError(f"not a valid age band: {value!r}")
    lo, hi = (int(part) for part in value.split("-"))
    return (lo + hi) / 2


_AGE_BANDS = frozenset(valid_age_bands())


class Sex(str, enum.Enum):
    female = "female"
    male = "male"


class DeploymentProfile(str, enum.Enum):
    research_public = "research-public"
    mask_only = "mask-only"
    on_premise = "on-premise"
    legacy = "legacy"


LEGACY_UPLOAD = "legacy_upload"


class Case(Base):
    __tablename__ = "cases"
    __table_args__ = (
        enum_check("cases", "sex", [e.value for e in Sex]),
        enum_check("cases", "deployment_profile", [e.value for e in DeploymentProfile]),
        UniqueConstraint("subject_label", name="uq_cases_subject_label"),
        UniqueConstraint("legacy_study_id", name="uq_cases_legacy_study_id"),
        Index("ix_cases_owner_user_id_created_at", "owner_user_id", "created_at"),
        Index("ix_cases_deleted_at", "deleted_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    # BIDS sub- label: first 12 hex characters of the id. Never entered by users. On a
    # unique violation the application generates a new id and retries.
    subject_label: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        default=lambda ctx: ctx.get_current_parameters()["id"].hex[:12],
    )
    session_label: Mapped[str] = mapped_column(
        String(16), nullable=False, default="acute", server_default="acute"
    )
    sex: Mapped[str | None] = mapped_column(String(8))
    age_band: Mapped[str | None] = mapped_column(String(7))
    deployment_profile: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        tz_datetime(), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        tz_datetime(), nullable=False, default=utcnow, onupdate=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(tz_datetime())
    keep_forever: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # Back-filled rows only (equal to id); dropped by migration 0013.
    legacy_study_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("studies.id"))
    # Fields used only by the legacy aliases (section 10.4); NULL for cases never touched
    # by an alias.
    legacy_attributes: Mapped[dict | None] = mapped_column(JSONType)

    input_files = relationship("InputFile", back_populates="case")
    runs = relationship("PipelineRun", back_populates="case")

    @validates("subject_label")
    def _validate_subject_label(self, _key: str, value: str) -> str:
        formats.check_format("subject_label", value)
        return value

    @validates("sex")
    def _validate_sex(self, _key: str, value: str | None) -> str | None:
        if value is not None and value not in {e.value for e in Sex}:
            raise ValueError("sex must be 'female', 'male' or absent")
        return value

    @validates("age_band")
    def _validate_age_band(self, _key: str, value: str | None) -> str | None:
        if value is not None and not is_valid_age_band(value):
            raise ValueError("age_band is not a valid band")
        return value


class InputFile(Base):
    __tablename__ = "input_files"
    __table_args__ = (
        CheckConstraint(
            f"role = '{LEGACY_UPLOAD}' OR sha256 IS NOT NULL",
            name="ck_input_files_sha256_required",
        ),
        CheckConstraint(
            f"role = '{LEGACY_UPLOAD}' OR size_bytes IS NOT NULL",
            name="ck_input_files_size_required",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name="ck_input_files_size_bytes"
        ),
        UniqueConstraint("case_id", "role", name="uq_input_files_case_id_role"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(64), nullable=False)
    artefact_type: Mapped[str] = mapped_column(String(64), nullable=False)
    # Relative to Settings.storage_root, '/'-separated, never containing '..'.
    storage_path: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    uploaded_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        tz_datetime(), nullable=False, default=utcnow
    )

    case = relationship("Case", back_populates="input_files")

    @validates("role", "artefact_type")
    def _validate_snake(self, _key: str, value: str) -> str:
        formats.check_format("snake", value)
        return value

    @validates("sha256")
    def _validate_sha256(self, _key: str, value: str | None) -> str | None:
        return formats.check_format("sha256hex", value)

    @validates("storage_path")
    def _validate_storage_path(self, _key: str, value: str) -> str:
        return check_relative_path(value)


def check_relative_path(value: str) -> str:
    """Storage paths are relative to the storage root, '/'-separated, without '..'."""
    parts = value.split("/")
    if not value or value.startswith("/") or "\\" in value or ".." in parts:
        raise ValueError("storage path must be relative, use '/' and not contain '..'")
    return value
