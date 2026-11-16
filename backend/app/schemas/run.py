"""Pydantic schemas of runs, slots, step attempts, artefacts and reports
(docs/data-model.md sections 4.3 to 4.8 and 4.10). Read models only; no endpoint yet."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.models import formats

Status = Literal["queued", "running", "awaiting_review", "succeeded", "failed", "canceled"]
Kebab = Annotated[str, StringConstraints(pattern=formats.KEBAB, max_length=64)]
Snake = Annotated[str, StringConstraints(pattern=formats.SNAKE, max_length=64)]
Semver = Annotated[str, StringConstraints(pattern=formats.SEMVER, max_length=64)]
Sha256Hex = Annotated[str, StringConstraints(pattern=formats.SHA256_HEX)]


class _Read(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class RunInputRead(_Read):
    role: Snake
    input_file_id: UUID


class RunSlotBundleRead(_Read):
    requirement_name: Snake
    reference_bundle_id: UUID


class RunSlotRead(_Read):
    id: UUID
    slot: Kebab
    position: int
    optional: bool
    status: Literal["selected", "skipped", "unavailable"]
    reason: Literal["condition_false", "upstream_absent", "no_implementation"] | None = None
    implementation_id: Kebab | None = None
    step_name: Kebab | None = None
    step_version: Semver | None = None
    image: str | None = None
    stub: bool | None = None
    chosen_by: Literal["user", "deployment_default", "pipeline_default"] | None = None
    effective_parameters: dict[str, Any] | None = None
    bundles: list[RunSlotBundleRead] = []


class StepRunRead(_Read):
    id: UUID
    pipeline_run_id: UUID
    run_slot_id: UUID
    attempt: int = Field(ge=1)
    trigger: Literal["initial", "auto_retry", "user_resume"]
    status: Status
    outcome: (
        Literal[
            "succeeded", "rejected", "failed", "canceled", "timed_out", "contract_violation"
        ]
        | None
    ) = None
    executor: Literal["subprocess", "docker", "slurm", "external"]
    runtime: Literal["docker", "apptainer", "none"]
    image: str | None = None
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    granted_cpus: int | None = None
    granted_memory_mb: int | None = None
    granted_gpus: int | None = None
    granted_wall_time_minutes: int | None = None
    usage_wall_seconds: Decimal | None = None
    usage_cpu_core_hours: Decimal | None = None
    usage_gpu_hours: Decimal | None = None
    usage_max_rss_mb: int | None = None
    step_error: dict[str, Any] | None = None
    failure_code: Snake | None = None
    failure_message: str | None = None
    log_truncated: bool = False


class PipelineRunRead(_Read):
    id: UUID
    case_id: UUID
    created_by_user_id: UUID
    pipeline_id: Kebab
    pipeline_version: Semver
    pipeline_sha256: Sha256Hex | None = None
    platform_version: str
    artefact_types_version: str
    input_set: Kebab
    seed: int = Field(ge=0, le=4294967295)
    status: Status
    requested_implementations: dict[str, str] = {}
    accounting_submitted_by: str
    accounting_submit_as: Literal["service-account", "per-user", "chargeback"]
    accounting_billing_account: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime
    cancel_requested_at: datetime | None = None
    resume_count: int = Field(default=0, ge=0)
    failure_code: Snake | None = None
    failure_message: str | None = None
    failed_step_run_id: UUID | None = None


class ArtifactRead(_Read):
    id: UUID
    case_id: UUID
    pipeline_run_id: UUID
    step_run_id: UUID | None = None
    origin: Literal["step", "review", "legacy"]
    port: Snake
    artefact_type: Snake
    sha256: Sha256Hex | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    synthetic: bool
    supersedes_artifact_id: UUID | None = None
    created_at: datetime


class ReportRead(_Read):
    id: UUID
    case_id: UUID
    pipeline_run_id: UUID
    step_run_id: UUID
    pdf_artifact_id: UUID
    json_artifact_id: UUID | None = None
    template: Kebab
    language: str
    content_sha256: Sha256Hex | None = None
    created_at: datetime
