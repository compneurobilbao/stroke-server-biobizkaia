"""Request and response models of the versioned API (`/api/v1`, card F0-30).

They build on the read models of ``schemas/case.py``, ``schemas/run.py`` and
``schemas/bundle.py`` (docs/data-model.md section 4). Nothing here carries a name,
an exact age, a free-text identifier or an uploaded file name.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models import formats
from app.schemas.case import CaseRead, CaseUpdate, InputFileRead
from app.schemas.run import (
    ArtifactRead,
    PipelineRunRead,
    ReportRead,
    RunInputRead,
    RunSlotRead,
    StepRunRead,
)

Kebab = Annotated[str, StringConstraints(pattern=formats.KEBAB, max_length=64)]
Snake = Annotated[str, StringConstraints(pattern=formats.SNAKE, max_length=64)]
Semver = Annotated[str, StringConstraints(pattern=formats.SEMVER, max_length=64)]


# ------------------------------------------------------------------ pipelines


class PortRead(BaseModel):
    port: str
    type: str
    optional: bool = False


class SlotImplementationRead(BaseModel):
    id: str
    title: str | None = None
    step_name: str
    step_version: str
    image: str
    stub: bool | None = None
    available: bool  # the step manifest is loaded in this deployment
    enabled: bool
    is_default: bool  # deployment default (slot_implementation_settings)
    is_pipeline_default: bool


class SlotRead(BaseModel):
    name: str
    title: str | None = None
    description: str | None = None
    position: int
    optional: bool
    when: dict[str, Any] | None = None
    consumes: list[PortRead]
    produces: list[PortRead]
    # What a run would use without a user choice, or null if the slot has no usable default.
    default_implementation: str | None = None
    implementations: list[SlotImplementationRead]


class PipelineInputRead(BaseModel):
    name: str
    type: str
    description: str | None = None


class InputSetRead(BaseModel):
    name: str
    inputs: list[str]
    description: str | None = None


class PipelineSummary(BaseModel):
    id: str
    version: str
    title: str
    description: str | None = None
    sha256: str
    inputs: list[PipelineInputRead]
    input_sets: list[InputSetRead]


class PipelineDetail(PipelineSummary):
    slots: list[SlotRead]


# ---------------------------------------------------------------------- cases


class CaseUpdateV1(CaseUpdate):
    """Metadata edits; ``keep_forever`` is accepted from administrators only."""

    keep_forever: bool | None = None


class CaseDetailRead(CaseRead):
    inputs: list[InputFileRead] = []


# ----------------------------------------------------------------------- runs


class RunCreate(BaseModel):
    """Start a run of a pipeline on the inputs currently stored in the case."""

    model_config = ConfigDict(extra="forbid")

    pipeline_id: Kebab
    pipeline_version: Semver | None = None  # default: the highest loaded version
    implementations: dict[Kebab, Kebab] = Field(
        default_factory=dict,
        description="Per-slot choice: slot name to implementation id.",
    )
    input_roles: list[Snake] | None = Field(
        default=None,
        description="Subset of the case's input roles; default: all of them.",
    )
    seed: int | None = Field(default=None, ge=0, le=4294967295)


class StepAttemptRead(StepRunRead):
    slot: str
    log_available: bool = False


class RunDetailRead(PipelineRunRead):
    cancel_requested: bool = False
    inputs: list[RunInputRead] = []
    slots: list[RunSlotRead] = []
    steps: list[StepAttemptRead] = []


class RunActionResponse(BaseModel):
    run_id: UUID
    status: str
    cancel_requested: bool = False
    changed: bool = True


class ArtifactV1Read(ArtifactRead):
    filename: str


class ReportV1Read(ReportRead):
    pdf_filename: str | None = None


# ---------------------------------------------------------------------- admin


class ImplementationAdminRead(BaseModel):
    pipeline_id: str
    slot: str
    implementation_id: str
    step_name: str
    step_version: str
    image: str
    enabled: bool
    is_default: bool
    updated_at: datetime | None = None
    updated_by_user_id: UUID | None = None


__all__ = [
    "ArtifactV1Read",
    "CaseDetailRead",
    "CaseUpdateV1",
    "ImplementationAdminRead",
    "PipelineDetail",
    "PipelineSummary",
    "ReportV1Read",
    "RunActionResponse",
    "RunCreate",
    "RunDetailRead",
    "SlotRead",
    "StepAttemptRead",
]
