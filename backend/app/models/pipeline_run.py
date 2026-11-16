"""`PipelineRun`, `RunInput`, `RunSlot`, `RunSlotBundle`, `StepRun`
(docs/data-model.md sections 4.3 to 4.7)."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship, validates

from app.db.base import Base
from app.models import formats
from app.models._common import JSONType, enum_check, sql_list, tz_datetime, utcnow


class RunStatus(str, enum.Enum):
    """Statuses of runs and of step attempts (the contract spells canceled with one l)."""

    queued = "queued"
    running = "running"
    awaiting_review = "awaiting_review"  # reserved for F1-05
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


ACTIVE_STATUSES = (
    RunStatus.queued.value,
    RunStatus.running.value,
    RunStatus.awaiting_review.value,
)
TERMINAL_STATUSES = (
    RunStatus.succeeded.value,
    RunStatus.failed.value,
    RunStatus.canceled.value,
)
_STATUS_VALUES = [e.value for e in RunStatus]


class SubmitAs(str, enum.Enum):
    service_account = "service-account"
    per_user = "per-user"
    chargeback = "chargeback"


class SlotStatus(str, enum.Enum):
    selected = "selected"
    skipped = "skipped"
    unavailable = "unavailable"


class SlotReason(str, enum.Enum):
    condition_false = "condition_false"
    upstream_absent = "upstream_absent"
    no_implementation = "no_implementation"


class ChosenBy(str, enum.Enum):
    user = "user"
    deployment_default = "deployment_default"
    pipeline_default = "pipeline_default"


class StepTrigger(str, enum.Enum):
    initial = "initial"
    auto_retry = "auto_retry"
    user_resume = "user_resume"


class StepOutcome(str, enum.Enum):
    succeeded = "succeeded"
    rejected = "rejected"
    failed = "failed"
    canceled = "canceled"
    timed_out = "timed_out"
    contract_violation = "contract_violation"


class ExecutorKind(str, enum.Enum):
    subprocess = "subprocess"
    docker = "docker"
    slurm = "slurm"
    external = "external"


class RuntimeKind(str, enum.Enum):
    docker = "docker"
    apptainer = "apptainer"
    none = "none"


class LegacyReviewStatus(str, enum.Enum):
    technical_only = "technical_only"
    reviewed = "reviewed"
    validated = "validated"


LEGACY_PIPELINE_ID = "legacy"
LEGACY_SLOT = "legacy"

_ACTIVE_SQL = f"status IN ({sql_list(ACTIVE_STATUSES)})"
_TERMINAL_SQL = f"status IN ({sql_list(TERMINAL_STATUSES)})"


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"
    __table_args__ = (
        enum_check("pipeline_runs", "status", _STATUS_VALUES),
        enum_check("pipeline_runs", "accounting_submit_as", [e.value for e in SubmitAs]),
        enum_check(
            "pipeline_runs",
            "legacy_clinical_review_status",
            [e.value for e in LegacyReviewStatus],
        ),
        CheckConstraint("seed BETWEEN 0 AND 4294967295", name="ck_pipeline_runs_seed"),
        CheckConstraint(
            f"pipeline_id = '{LEGACY_PIPELINE_ID}' OR pipeline_sha256 IS NOT NULL",
            name="ck_pipeline_runs_pipeline_sha256",
        ),
        CheckConstraint(
            f"({_TERMINAL_SQL}) = (finished_at IS NOT NULL)",
            name="ck_pipeline_runs_finished_at",
        ),
        CheckConstraint("resume_count >= 0", name="ck_pipeline_runs_resume_count"),
        Index("ix_pipeline_runs_case_id_created_at", "case_id", "created_at"),
        Index("ix_pipeline_runs_status", "status"),
        Index("ix_pipeline_runs_created_by_user_id", "created_by_user_id"),
        Index("ix_pipeline_runs_pipeline", "pipeline_id", "pipeline_version"),
        Index("ix_pipeline_runs_billing", "accounting_billing_account", "created_at"),
        # At most one active run per case.
        Index(
            "uq_pipeline_runs_active_case",
            "case_id",
            unique=True,
            postgresql_where=text(_ACTIVE_SQL),
            sqlite_where=text(_ACTIVE_SQL),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id"), nullable=False)
    created_by_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    pipeline_id: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_version: Mapped[str] = mapped_column(String(64), nullable=False)
    pipeline_sha256: Mapped[str | None] = mapped_column(String(64))
    platform_version: Mapped[str] = mapped_column(String(64), nullable=False)
    artefact_types_version: Mapped[str] = mapped_column(String(64), nullable=False)
    input_set: Mapped[str] = mapped_column(String(64), nullable=False)
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_implementations: Mapped[dict] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    accounting_submitted_by: Mapped[str] = mapped_column(String(80), nullable=False)
    accounting_submit_as: Mapped[str] = mapped_column(String(20), nullable=False)
    accounting_billing_account: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        tz_datetime(), nullable=False, default=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(tz_datetime())
    finished_at: Mapped[datetime | None] = mapped_column(tz_datetime())
    updated_at: Mapped[datetime] = mapped_column(
        tz_datetime(), nullable=False, default=utcnow, onupdate=utcnow
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(tz_datetime())
    cancel_requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id")
    )
    resume_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(Text)
    # The two tables reference each other: the FK is added after both exist.
    failed_step_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey(
            "step_runs.id",
            use_alter=True,
            name="fk_pipeline_runs_failed_step_run_id_step_runs",
        )
    )
    provenance_path: Mapped[str | None] = mapped_column(Text)
    orchestrator_task_id: Mapped[str | None] = mapped_column(String(255))
    # Used only by the legacy clinical-review alias; dropped by migration 0013.
    legacy_clinical_review_status: Mapped[str | None] = mapped_column(String(32))

    case = relationship("Case", back_populates="runs")
    inputs = relationship(
        "RunInput",
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    slots = relationship(
        "RunSlot",
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="RunSlot.position",
    )
    steps = relationship(
        "StepRun",
        back_populates="run",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="StepRun.pipeline_run_id",
    )

    @validates("pipeline_id", "input_set")
    def _validate_kebab(self, _key: str, value: str) -> str:
        formats.check_format("kebab", value)
        return value

    @validates("pipeline_version")
    def _validate_semver(self, _key: str, value: str) -> str:
        formats.check_format("semver", value)
        return value

    @validates("pipeline_sha256")
    def _validate_sha(self, _key: str, value: str | None) -> str | None:
        return formats.check_format("sha256hex", value)

    @validates("failure_code")
    def _validate_snake(self, _key: str, value: str | None) -> str | None:
        return formats.check_format("snake", value)


class RunInput(Base):
    __tablename__ = "run_inputs"
    __table_args__ = (Index("ix_run_inputs_input_file_id", "input_file_id"),)

    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(64), primary_key=True)
    input_file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("input_files.id"), nullable=False
    )

    run = relationship("PipelineRun", back_populates="inputs")
    input_file = relationship("InputFile")

    @validates("role")
    def _validate_role(self, _key: str, value: str) -> str:
        formats.check_format("snake", value)
        return value


_SLOT_NULL_WHEN_NOT_SELECTED = (
    "implementation_id",
    "step_name",
    "step_version",
    "image",
    "stub",
    "chosen_by",
    "effective_parameters",
)
_SLOT_SELECTED_SQL = (
    "(status = 'selected' AND reason IS NULL"
    " AND implementation_id IS NOT NULL AND step_name IS NOT NULL"
    " AND step_version IS NOT NULL AND stub IS NOT NULL AND chosen_by IS NOT NULL"
    " AND effective_parameters IS NOT NULL"
    f" AND (image IS NOT NULL OR slot = '{LEGACY_SLOT}'))"
)
_SLOT_OTHER_SQL = (
    "(status <> 'selected' AND reason IS NOT NULL AND "
    + " AND ".join(f"{c} IS NULL" for c in _SLOT_NULL_WHEN_NOT_SELECTED)
    + ")"
)


class RunSlot(Base):
    """Resolution of one pipeline slot for one run (RunProvenance.slots[]); immutable."""

    __tablename__ = "run_slots"
    __table_args__ = (
        enum_check("run_slots", "status", [e.value for e in SlotStatus]),
        enum_check("run_slots", "reason", [e.value for e in SlotReason]),
        enum_check("run_slots", "chosen_by", [e.value for e in ChosenBy]),
        CheckConstraint(
            f"{_SLOT_SELECTED_SQL} OR {_SLOT_OTHER_SQL}", name="ck_run_slots_selected"
        ),
        UniqueConstraint("pipeline_run_id", "slot", name="uq_run_slots_run_slot"),
        UniqueConstraint("pipeline_run_id", "position", name="uq_run_slots_run_position"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False
    )
    slot: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    optional: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(32))
    implementation_id: Mapped[str | None] = mapped_column(String(64))
    step_name: Mapped[str | None] = mapped_column(String(64))
    step_version: Mapped[str | None] = mapped_column(String(64))
    image: Mapped[str | None] = mapped_column(String(300))
    stub: Mapped[bool | None] = mapped_column(Boolean)
    chosen_by: Mapped[str | None] = mapped_column(String(24))
    effective_parameters: Mapped[dict | None] = mapped_column(JSONType)

    run = relationship("PipelineRun", back_populates="slots")
    bundles = relationship(
        "RunSlotBundle",
        back_populates="run_slot",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    step_runs = relationship(
        "StepRun",
        back_populates="run_slot",
        cascade="all, delete-orphan",
        passive_deletes=True,
        foreign_keys="StepRun.run_slot_id",
        order_by="StepRun.attempt",
    )

    @validates("slot", "implementation_id", "step_name")
    def _validate_kebab(self, _key: str, value: str | None) -> str | None:
        return formats.check_format("kebab", value)

    @validates("step_version")
    def _validate_semver(self, _key: str, value: str | None) -> str | None:
        return formats.check_format("semver", value)

    @validates("image")
    def _validate_image(self, _key: str, value: str | None) -> str | None:
        return formats.check_format("image_ref", value)


class RunSlotBundle(Base):
    __tablename__ = "run_slot_bundles"
    __table_args__ = (
        Index("ix_run_slot_bundles_reference_bundle_id", "reference_bundle_id"),
    )

    run_slot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run_slots.id", ondelete="CASCADE"), primary_key=True
    )
    requirement_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    reference_bundle_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reference_bundles.id"), nullable=False
    )

    run_slot = relationship("RunSlot", back_populates="bundles")
    reference_bundle = relationship("ReferenceBundle")

    @validates("requirement_name")
    def _validate_name(self, _key: str, value: str) -> str:
        formats.check_format("snake", value)
        return value


class StepRun(Base):
    """One execution attempt of a selected slot (RunProvenance.steps[])."""

    __tablename__ = "step_runs"
    __table_args__ = (
        enum_check("step_runs", "trigger", [e.value for e in StepTrigger]),
        enum_check("step_runs", "status", _STATUS_VALUES),
        enum_check("step_runs", "outcome", [e.value for e in StepOutcome]),
        enum_check("step_runs", "executor", [e.value for e in ExecutorKind]),
        enum_check("step_runs", "runtime", [e.value for e in RuntimeKind]),
        CheckConstraint("attempt >= 1", name="ck_step_runs_attempt"),
        CheckConstraint(
            "(status IN ('queued', 'running') AND outcome IS NULL)"
            " OR (status = 'awaiting_review' AND outcome IS NOT NULL AND outcome = 'succeeded')"
            " OR (status = 'succeeded' AND outcome IS NOT NULL AND outcome = 'succeeded')"
            " OR (status = 'failed' AND outcome IS NOT NULL AND outcome IN"
            " ('rejected', 'failed', 'timed_out', 'contract_violation'))"
            " OR (status = 'canceled' AND outcome IS NOT NULL AND outcome = 'canceled')",
            name="ck_step_runs_status_outcome",
        ),
        CheckConstraint(
            f"({_TERMINAL_SQL}) = (finished_at IS NOT NULL)",
            name="ck_step_runs_finished_at",
        ),
        CheckConstraint(
            "executor NOT IN ('docker', 'slurm') OR image IS NOT NULL",
            name="ck_step_runs_image",
        ),
        CheckConstraint(
            "executor IN ('subprocess', 'external') OR (granted_cpus IS NOT NULL"
            " AND granted_memory_mb IS NOT NULL AND granted_gpus IS NOT NULL"
            " AND granted_wall_time_minutes IS NOT NULL)",
            name="ck_step_runs_granted_resources",
        ),
        # Portable form of: outcome IS DISTINCT FROM 'rejected' OR step_error IS NOT NULL
        CheckConstraint(
            "outcome IS NULL OR outcome <> 'rejected' OR step_error IS NOT NULL",
            name="ck_step_runs_step_error",
        ),
        UniqueConstraint("run_slot_id", "attempt", name="uq_step_runs_slot_attempt"),
        UniqueConstraint("legacy_job_id", name="uq_step_runs_legacy_job_id"),
        Index(
            "uq_step_runs_one_active",
            "run_slot_id",
            unique=True,
            postgresql_where=text(_ACTIVE_SQL),
            sqlite_where=text(_ACTIVE_SQL),
        ),
        Index(
            "uq_step_runs_one_success",
            "run_slot_id",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
            sqlite_where=text("status = 'succeeded'"),
        ),
        Index("ix_step_runs_pipeline_run_id", "pipeline_run_id"),
        Index(
            "ix_step_runs_active",
            "status",
            postgresql_where=text("status IN ('queued', 'running')"),
            sqlite_where=text("status IN ('queued', 'running')"),
        ),
        Index(
            "ix_step_runs_external_job",
            "executor",
            "external_job_id",
            postgresql_where=text("external_job_id IS NOT NULL"),
            sqlite_where=text("external_job_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline_runs.id", ondelete="CASCADE"), nullable=False
    )
    run_slot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("run_slots.id", ondelete="CASCADE"), nullable=False
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    trigger: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(24))
    executor: Mapped[str] = mapped_column(String(16), nullable=False)
    runtime: Mapped[str] = mapped_column(String(16), nullable=False)
    image: Mapped[str | None] = mapped_column(String(300))
    sif_sha256: Mapped[str | None] = mapped_column(String(64))
    external_job_id: Mapped[str | None] = mapped_column(String(128))
    # Opaque to everything except the executor that wrote it; no host or user names.
    executor_state: Mapped[dict | None] = mapped_column(JSONType)
    queued_at: Mapped[datetime] = mapped_column(
        tz_datetime(), nullable=False, default=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(tz_datetime())
    finished_at: Mapped[datetime | None] = mapped_column(tz_datetime())
    exit_code: Mapped[int | None] = mapped_column(Integer)
    granted_cpus: Mapped[int | None] = mapped_column(Integer)
    granted_memory_mb: Mapped[int | None] = mapped_column(Integer)
    granted_gpus: Mapped[int | None] = mapped_column(Integer)
    granted_wall_time_minutes: Mapped[int | None] = mapped_column(Integer)
    usage_wall_seconds: Mapped[Decimal | None] = mapped_column(Numeric(14, 3))
    usage_cpu_core_hours: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    usage_gpu_hours: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    usage_max_rss_mb: Mapped[int | None] = mapped_column(Integer)
    step_provenance: Mapped[dict | None] = mapped_column(JSONType)
    step_provenance_sha256: Mapped[str | None] = mapped_column(String(64))
    step_error: Mapped[dict | None] = mapped_column(JSONType)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    failure_message: Mapped[str | None] = mapped_column(Text)
    log_path: Mapped[str | None] = mapped_column(Text)
    log_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    orchestrator_task_id: Mapped[str | None] = mapped_column(String(255))
    cancel_requested_at: Mapped[datetime | None] = mapped_column(tz_datetime())
    # Back-filled rows only; dropped by migration 0013.
    legacy_job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processing_jobs.id")
    )

    run = relationship(
        "PipelineRun", back_populates="steps", foreign_keys=[pipeline_run_id]
    )
    run_slot = relationship(
        "RunSlot", back_populates="step_runs", foreign_keys=[run_slot_id]
    )

    @validates("image")
    def _validate_image(self, _key: str, value: str | None) -> str | None:
        return formats.check_format("image_ref", value)

    @validates("sif_sha256", "step_provenance_sha256")
    def _validate_sha(self, _key: str, value: str | None) -> str | None:
        return formats.check_format("sha256hex", value)

    @validates("failure_code")
    def _validate_snake(self, _key: str, value: str | None) -> str | None:
        return formats.check_format("snake", value)
