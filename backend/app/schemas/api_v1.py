"""Request and response models of the versioned API (`/api/v1`, card F0-30).

They build on the read models of ``schemas/case.py``, ``schemas/run.py`` and
``schemas/bundle.py`` (docs/data-model.md section 4). Nothing here carries a name,
an exact age, a free-text identifier or an uploaded file name.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_serializer,
    model_validator,
)

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
Sha256Hex = Annotated[str, StringConstraints(pattern=formats.SHA256_HEX)]
# A slot, ``slot.port`` or an artefact type name. Not matched against a pattern here:
# an unknown name is refused with the code ``unknown_target`` (run-plans.md 3.3).
TargetName = Annotated[str, StringConstraints(min_length=1, max_length=128)]


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


# ----------------------------------------------------------------------- plans


class PresetRefIn(BaseModel):
    """A preset named in a request; without ``version``, the highest enabled one."""

    model_config = ConfigDict(extra="forbid")

    id: Kebab
    version: Semver | None = None


class TargetsIn(BaseModel):
    """Free-mode targets (run-plans.md 3.2): slots, ``slot.port`` or artefact types."""

    model_config = ConfigDict(extra="forbid")

    required: list[TargetName] = Field(default_factory=list, max_length=64)
    if_available: list[TargetName] = Field(default_factory=list, max_length=64)


class PlanRequestIn(BaseModel):
    """Dry run of a run request: the plan it would get, nothing written."""

    model_config = ConfigDict(extra="forbid")

    input_roles: list[Snake] = Field(
        min_length=1,
        max_length=32,
        description="Input roles the run would have; they decide the input set.",
    )
    preset: PresetRefIn | None = None
    targets: TargetsIn | None = None
    implementations: dict[Kebab, Kebab] = Field(default_factory=dict)


class PlanReasonRead(BaseModel):
    """One reason a plan request is refused (run-plans.md 3.3); fields only when relevant."""

    code: str
    message: str
    target: str | None = None
    slot: str | None = None
    port: str | None = None
    required_by: str | None = None

    @model_serializer(mode="wrap")
    def _omit_empty(self, handler):
        # Fields only when relevant, as in the error body of a refused run request.
        return {k: v for k, v in handler(self).items() if v is not None}


class PlanSlotRead(BaseModel):
    name: str
    status: Literal["selected", "skipped", "unavailable"]
    reason: (
        Literal[
            "condition_false", "upstream_absent", "no_implementation", "not_requested"
        ]
        | None
    ) = None
    plan_role: Literal["target", "upstream", "if_available"] | None = None
    # The planned slot that needs this one (for ``upstream`` and ``if_available``
    # slots), from the pipeline wiring.
    required_by: str | None = None
    implementation: str | None = None
    chosen_by: (
        Literal["user", "deployment_default", "pipeline_default", "preset"] | None
    ) = None


class PlanPresetRead(BaseModel):
    id: Kebab
    version: Semver
    sha256: Sha256Hex


class PlanTargetsRead(BaseModel):
    """The request as resolved: ``slot.port`` lists; both empty for the full plan."""

    required: list[str] = []
    if_available: list[str] = []


class PlanPreviewRead(BaseModel):
    feasible: bool
    mode: Literal["full", "preset", "free"]
    input_set: str
    preset: PlanPresetRead | None = None
    targets: PlanTargetsRead = PlanTargetsRead()
    # One entry per pipeline slot in pipeline order; empty when the request is refused
    # before a plan can be computed (for example an unknown preset).
    slots: list[PlanSlotRead] = []
    reasons: list[PlanReasonRead] = []


class RunPlanRead(BaseModel):
    """The plan request of a run as recorded (``pipeline_runs.plan_mode`` and siblings)."""

    mode: Literal["full", "preset", "free"]
    preset: PlanPresetRead | None = None
    targets: PlanTargetsRead = PlanTargetsRead()


class PresetChoiceRead(BaseModel):
    fixed: str | None = None
    default: str | None = None


class PresetPlanRead(BaseModel):
    """The plan of a preset for one input set, with the deployment's current enablement."""

    input_set: str
    available: bool
    slots: list[PlanSlotRead]
    reasons: list[PlanReasonRead] = []


class PresetRead(BaseModel):
    id: Kebab
    version: Semver
    sha256: Sha256Hex
    title: str
    description: str | None = None
    input_sets: list[str]
    targets: PlanTargetsRead
    implementations: dict[str, PresetChoiceRead] = {}
    is_default: bool
    # True when the plan of at least one accepted input set can run now.
    available: bool
    plans: list[PresetPlanRead]


class PresetSettingUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool
    is_default: bool = False

    @model_validator(mode="after")
    def _default_must_be_enabled(self) -> PresetSettingUpdate:
        if self.is_default and not self.enabled:
            raise ValueError("a default preset must be enabled")
        return self


class PresetAdminRead(BaseModel):
    pipeline_id: str
    preset_id: str
    title: str
    versions: list[str]  # versions of the preset in the loaded files
    pipeline_versions: list[str]  # loaded pipeline versions that offer it
    enabled: bool
    is_default: bool
    updated_at: datetime | None = None
    updated_by_user_id: UUID | None = None


class ConfigRead(BaseModel):
    deployment_profile: str
    run_plan_policy: Literal["presets_only", "free_for_admins", "free_for_researchers"]
    # Whether the caller may start runs with targets; the UI offers free mode only then.
    free_mode_allowed: bool


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
    preset: PresetRefIn | None = Field(
        default=None, description="Run a reviewed preset (exclusive with targets)."
    )
    targets: TargetsIn | None = Field(
        default=None,
        description="Free mode: the outputs wanted (exclusive with preset); "
        "403 unless the deployment policy allows it.",
    )


class StepAttemptRead(StepRunRead):
    slot: str
    log_available: bool = False


class RunDetailRead(PipelineRunRead):
    plan: RunPlanRead
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
    "ConfigRead",
    "ImplementationAdminRead",
    "PipelineDetail",
    "PipelineSummary",
    "PlanPreviewRead",
    "PlanRequestIn",
    "PresetAdminRead",
    "PresetRead",
    "PresetSettingUpdate",
    "ReportV1Read",
    "RunActionResponse",
    "RunCreate",
    "RunDetailRead",
    "SlotRead",
    "StepAttemptRead",
]
