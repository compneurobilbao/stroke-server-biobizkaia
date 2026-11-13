"""Pydantic schemas of reference bundles and slot implementation settings
(docs/data-model.md sections 4.9 and 4.11). Read and update models only; no endpoint yet."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from app.models import formats

Kebab = Annotated[str, StringConstraints(pattern=formats.KEBAB, max_length=64)]


class ReferenceBundleRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    bundle_id: Kebab
    version: Annotated[str, StringConstraints(pattern=formats.SEMVER, max_length=64)]
    bundle_type: Kebab
    title: str
    content_digest: Annotated[str, StringConstraints(pattern=formats.DIGEST)]
    manifest_sha256: Annotated[str, StringConstraints(pattern=formats.SHA256_HEX)]
    synthetic: bool
    licence_spdx: str
    redistributable: bool
    citation: str | None = None
    # The ORM attribute is metadata_ (metadata is reserved by SQLAlchemy).
    metadata: dict[str, Any] = Field(default_factory=dict, validation_alias="metadata_")
    status: Literal["active", "withdrawn"]
    verification_status: Literal["verified", "failed"]
    registered_at: datetime
    last_full_verification_at: datetime
    withdrawn_at: datetime | None = None


class SlotImplementationSettingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    pipeline_id: Kebab
    slot: Kebab
    implementation_id: Kebab
    enabled: bool
    is_default: bool
    updated_at: datetime
    updated_by_user_id: UUID | None = None


class SlotImplementationSettingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    is_default: bool = False

    @model_validator(mode="after")
    def _default_must_be_enabled(self) -> "SlotImplementationSettingUpdate":
        if self.is_default and not self.enabled:
            raise ValueError("a default implementation must be enabled")
        return self
