"""Pydantic schemas of cases and input files (docs/data-model.md sections 4.1 and 4.2).

No endpoint uses them yet (the /api/v1 routes arrive with F0-30). Format rules that
PostgreSQL enforces with regex CHECKs are enforced here in all environments.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.models import formats
from app.models.case import is_valid_age_band

Snake = Annotated[str, StringConstraints(pattern=formats.SNAKE, max_length=64)]
Sha256Hex = Annotated[str, StringConstraints(pattern=formats.SHA256_HEX)]


def validate_age_band(value: str | None) -> str | None:
    if value is not None and not is_valid_age_band(value):
        raise ValueError(
            "age_band must be a 5-year band such as 65-69 (ages 90 and above: 90-104)"
        )
    return value


class CaseCreate(BaseModel):
    """Metadata a client may supply. An exact age is never accepted, only a band."""

    model_config = ConfigDict(extra="forbid")

    sex: Literal["female", "male"] | None = None
    age_band: str | None = None

    _age_band = field_validator("age_band")(validate_age_band)


class CaseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sex: Literal["female", "male"] | None = None
    age_band: str | None = None

    _age_band = field_validator("age_band")(validate_age_band)


class CaseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    owner_user_id: UUID
    subject_label: str = Field(pattern=formats.SUBJECT_LABEL)
    session_label: str
    sex: Literal["female", "male"] | None = None
    age_band: str | None = None
    deployment_profile: str
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    keep_forever: bool = False


class InputFileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    case_id: UUID
    role: Snake
    artefact_type: Snake
    sha256: Sha256Hex | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    uploaded_by_user_id: UUID
    created_at: datetime
