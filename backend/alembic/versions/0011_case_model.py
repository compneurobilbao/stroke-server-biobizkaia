"""case, run, step attempt and artefact data model (stage 1 of the migration plan)

Creates the Phase 0 tables of docs/data-model.md (ADR-0016), adds the new link columns to
audit_events, notifications and share_links, adds the role CHECK to users, and back-fills
the new tables from the legacy studies and processing_jobs rows. The legacy tables are not
modified, so the legacy code keeps working unchanged.

The back-fill is idempotent: every new row has an id derived from the legacy ids (see
LEGACY_NS below) and is inserted with ON CONFLICT DO NOTHING, so running it again changes
nothing. Legacy paths are made relative to Settings.storage_root; if a legacy path lies
outside it, or the legacy data violates a rule of the new model, the migration aborts and
lists the affected ids.

Downgrade drops everything this migration created. It refuses to run if rows exist that
cannot be reconstructed from the legacy tables, unless called with
`alembic -x allow_data_loss=true downgrade ...`.

Revision ID: 0011_case_model
Revises: 0010_job_cancel_requested_at
Create Date: 2026-11-13
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0011_case_model"
down_revision = "0010_job_cancel_requested_at"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

LEGACY_NS = uuid.uuid5(uuid.NAMESPACE_URL, "urn:stroke-server:legacy")
LEGACY_UPLOAD = "legacy_upload"
ROLES = ("admin", "researcher", "reviewer", "coordinator", "service")

# --- format rules (copy of app.models.formats; a migration must not change with it) -----
SHA256_HEX = r"^[0-9a-f]{64}$"
DIGEST = r"^sha256:[0-9a-f]{64}$"
IMAGE_REF = (
    r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+@sha256:[a-f0-9]{64}$"
)
KEBAB = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
SNAKE = r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$"
SEMVER = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$"
)
SUBJECT_LABEL = r"^[0-9a-f]{12}$"
AGE_BAND_FORMAT = r"^[0-9]{1,3}-[0-9]{1,3}$"

# (table, column, pattern): PostgreSQL-only regex CHECKs named ck_<table>_<column>_format.
FORMAT_CHECKS = [
    ("cases", "subject_label", SUBJECT_LABEL),
    ("cases", "age_band", AGE_BAND_FORMAT),
    ("input_files", "role", SNAKE),
    ("input_files", "artefact_type", SNAKE),
    ("input_files", "sha256", SHA256_HEX),
    ("pipeline_runs", "pipeline_id", KEBAB),
    ("pipeline_runs", "pipeline_version", SEMVER),
    ("pipeline_runs", "pipeline_sha256", SHA256_HEX),
    ("pipeline_runs", "input_set", KEBAB),
    ("pipeline_runs", "failure_code", SNAKE),
    ("run_inputs", "role", SNAKE),
    ("run_slots", "slot", KEBAB),
    ("run_slots", "implementation_id", KEBAB),
    ("run_slots", "step_name", KEBAB),
    ("run_slots", "step_version", SEMVER),
    ("run_slots", "image", IMAGE_REF),
    ("run_slot_bundles", "requirement_name", SNAKE),
    ("step_runs", "image", IMAGE_REF),
    ("step_runs", "sif_sha256", SHA256_HEX),
    ("step_runs", "step_provenance_sha256", SHA256_HEX),
    ("step_runs", "failure_code", SNAKE),
    ("artifacts", "port", SNAKE),
    ("artifacts", "artefact_type", SNAKE),
    ("artifacts", "sha256", SHA256_HEX),
    ("reference_bundles", "bundle_id", KEBAB),
    ("reference_bundles", "version", SEMVER),
    ("reference_bundles", "bundle_type", KEBAB),
    ("reference_bundles", "content_digest", DIGEST),
    ("reference_bundles", "manifest_sha256", SHA256_HEX),
    ("reports", "template", KEBAB),
    ("reports", "content_sha256", SHA256_HEX),
    ("slot_implementation_settings", "pipeline_id", KEBAB),
    ("slot_implementation_settings", "slot", KEBAB),
    ("slot_implementation_settings", "implementation_id", KEBAB),
]

# --- schema helpers ----------------------------------------------------------------------
UUID = sa.Uuid()
TS = sa.DateTime(timezone=True)
JSONB = sa.JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)
FALSE = sa.text("false")

STATUSES = ("queued", "running", "awaiting_review", "succeeded", "failed", "canceled")
ACTIVE = ("queued", "running", "awaiting_review")
TERMINAL = ("succeeded", "failed", "canceled")


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _enum(table: str, column: str, values) -> sa.CheckConstraint:
    return sa.CheckConstraint(f"{column} IN ({_in(values)})", name=f"ck_{table}_{column}")


def _fk(table: str, column: str, target: str, ondelete: str | None = None, **kw):
    return sa.ForeignKeyConstraint(
        [column],
        [f"{target}.id"],
        name=f"fk_{table}_{column}_{target}",
        ondelete=ondelete,
        **kw,
    )


def _partial_unique(name: str, table: str, columns: list[str], where: str) -> None:
    op.create_index(
        name,
        table,
        columns,
        unique=True,
        postgresql_where=sa.text(where),
        sqlite_where=sa.text(where),
    )


def _partial_index(name: str, table: str, columns: list[str], where: str) -> None:
    op.create_index(
        name,
        table,
        columns,
        postgresql_where=sa.text(where),
        sqlite_where=sa.text(where),
    )


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


# --- upgrade -----------------------------------------------------------------------------
def upgrade() -> None:
    _check_roles()
    _create_tables()
    _alter_existing_tables()
    if _is_postgresql():
        for table, column, pattern in FORMAT_CHECKS:
            op.create_check_constraint(
                f"ck_{table}_{column}_format", table, f"{column} ~ '{pattern}'"
            )
    from app.core.config import get_settings

    backfill(op.get_bind(), Path(get_settings().storage_root))


def _check_roles() -> None:
    bad = (
        op.get_bind()
        .execute(
            sa.text(f"SELECT DISTINCT role FROM users WHERE role NOT IN ({_in(ROLES)})")
        )
        .scalars()
        .all()
    )
    if bad:
        raise RuntimeError(f"users.role has values outside the accepted set: {bad}")


def _create_tables() -> None:
    op.create_table(
        "cases",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("owner_user_id", UUID, nullable=False),
        sa.Column("subject_label", sa.String(12), nullable=False),
        sa.Column("session_label", sa.String(16), nullable=False, server_default="acute"),
        sa.Column("sex", sa.String(8)),
        sa.Column("age_band", sa.String(7)),
        sa.Column("deployment_profile", sa.String(32), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("deleted_at", TS),
        sa.Column("keep_forever", sa.Boolean(), nullable=False, server_default=FALSE),
        sa.Column("legacy_study_id", UUID),
        sa.Column("legacy_attributes", JSONB),
        _fk("cases", "owner_user_id", "users"),
        _fk("cases", "legacy_study_id", "studies"),
        _enum("cases", "sex", ("female", "male")),
        _enum(
            "cases",
            "deployment_profile",
            ("research-public", "mask-only", "on-premise", "legacy"),
        ),
        sa.UniqueConstraint("subject_label", name="uq_cases_subject_label"),
        sa.UniqueConstraint("legacy_study_id", name="uq_cases_legacy_study_id"),
    )
    op.create_index(
        "ix_cases_owner_user_id_created_at", "cases", ["owner_user_id", "created_at"]
    )
    op.create_index("ix_cases_deleted_at", "cases", ["deleted_at"])

    op.create_table(
        "input_files",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("artefact_type", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("uploaded_by_user_id", UUID, nullable=False),
        sa.Column("created_at", TS, nullable=False),
        _fk("input_files", "case_id", "cases"),
        _fk("input_files", "uploaded_by_user_id", "users"),
        sa.CheckConstraint(
            f"role = '{LEGACY_UPLOAD}' OR sha256 IS NOT NULL",
            name="ck_input_files_sha256_required",
        ),
        sa.CheckConstraint(
            f"role = '{LEGACY_UPLOAD}' OR size_bytes IS NOT NULL",
            name="ck_input_files_size_required",
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name="ck_input_files_size_bytes"
        ),
        sa.UniqueConstraint("case_id", "role", name="uq_input_files_case_id_role"),
    )

    op.create_table(
        "pipeline_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("created_by_user_id", UUID, nullable=False),
        sa.Column("pipeline_id", sa.String(64), nullable=False),
        sa.Column("pipeline_version", sa.String(64), nullable=False),
        sa.Column("pipeline_sha256", sa.String(64)),
        sa.Column("platform_version", sa.String(64), nullable=False),
        sa.Column("artefact_types_version", sa.String(64), nullable=False),
        sa.Column("input_set", sa.String(64), nullable=False),
        sa.Column("seed", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("requested_implementations", JSONB, nullable=False),
        sa.Column("accounting_submitted_by", sa.String(80), nullable=False),
        sa.Column("accounting_submit_as", sa.String(20), nullable=False),
        sa.Column("accounting_billing_account", sa.String(128)),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("started_at", TS),
        sa.Column("finished_at", TS),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("cancel_requested_at", TS),
        sa.Column("cancel_requested_by_user_id", UUID),
        sa.Column("resume_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("failure_message", sa.Text()),
        sa.Column("failed_step_run_id", UUID),
        sa.Column("provenance_path", sa.Text()),
        sa.Column("orchestrator_task_id", sa.String(255)),
        sa.Column("legacy_clinical_review_status", sa.String(32)),
        _fk("pipeline_runs", "case_id", "cases"),
        _fk("pipeline_runs", "created_by_user_id", "users"),
        _fk("pipeline_runs", "cancel_requested_by_user_id", "users"),
        _enum("pipeline_runs", "status", STATUSES),
        _enum(
            "pipeline_runs",
            "accounting_submit_as",
            ("service-account", "per-user", "chargeback"),
        ),
        _enum(
            "pipeline_runs",
            "legacy_clinical_review_status",
            ("technical_only", "reviewed", "validated"),
        ),
        sa.CheckConstraint("seed BETWEEN 0 AND 4294967295", name="ck_pipeline_runs_seed"),
        sa.CheckConstraint(
            "pipeline_id = 'legacy' OR pipeline_sha256 IS NOT NULL",
            name="ck_pipeline_runs_pipeline_sha256",
        ),
        sa.CheckConstraint(
            f"(status IN ({_in(TERMINAL)})) = (finished_at IS NOT NULL)",
            name="ck_pipeline_runs_finished_at",
        ),
        sa.CheckConstraint("resume_count >= 0", name="ck_pipeline_runs_resume_count"),
    )
    op.create_index(
        "ix_pipeline_runs_case_id_created_at", "pipeline_runs", ["case_id", "created_at"]
    )
    op.create_index("ix_pipeline_runs_status", "pipeline_runs", ["status"])
    op.create_index(
        "ix_pipeline_runs_created_by_user_id", "pipeline_runs", ["created_by_user_id"]
    )
    op.create_index(
        "ix_pipeline_runs_pipeline", "pipeline_runs", ["pipeline_id", "pipeline_version"]
    )
    op.create_index(
        "ix_pipeline_runs_billing",
        "pipeline_runs",
        ["accounting_billing_account", "created_at"],
    )
    _partial_unique(
        "uq_pipeline_runs_active_case",
        "pipeline_runs",
        ["case_id"],
        f"status IN ({_in(ACTIVE)})",
    )

    op.create_table(
        "run_inputs",
        sa.Column("pipeline_run_id", UUID, primary_key=True),
        sa.Column("role", sa.String(64), primary_key=True),
        sa.Column("input_file_id", UUID, nullable=False),
        _fk("run_inputs", "pipeline_run_id", "pipeline_runs", ondelete="CASCADE"),
        _fk("run_inputs", "input_file_id", "input_files"),
    )
    op.create_index("ix_run_inputs_input_file_id", "run_inputs", ["input_file_id"])

    selected = (
        "(status = 'selected' AND reason IS NULL AND implementation_id IS NOT NULL"
        " AND step_name IS NOT NULL AND step_version IS NOT NULL AND stub IS NOT NULL"
        " AND chosen_by IS NOT NULL AND effective_parameters IS NOT NULL"
        " AND (image IS NOT NULL OR slot = 'legacy'))"
    )
    other = (
        "(status <> 'selected' AND reason IS NOT NULL AND implementation_id IS NULL"
        " AND step_name IS NULL AND step_version IS NULL AND image IS NULL"
        " AND stub IS NULL AND chosen_by IS NULL AND effective_parameters IS NULL)"
    )
    op.create_table(
        "run_slots",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("pipeline_run_id", UUID, nullable=False),
        sa.Column("slot", sa.String(64), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("optional", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(32)),
        sa.Column("implementation_id", sa.String(64)),
        sa.Column("step_name", sa.String(64)),
        sa.Column("step_version", sa.String(64)),
        sa.Column("image", sa.String(300)),
        sa.Column("stub", sa.Boolean()),
        sa.Column("chosen_by", sa.String(24)),
        sa.Column("effective_parameters", JSONB),
        _fk("run_slots", "pipeline_run_id", "pipeline_runs", ondelete="CASCADE"),
        _enum("run_slots", "status", ("selected", "skipped", "unavailable")),
        _enum(
            "run_slots",
            "reason",
            ("condition_false", "upstream_absent", "no_implementation"),
        ),
        _enum(
            "run_slots",
            "chosen_by",
            ("user", "deployment_default", "pipeline_default"),
        ),
        sa.CheckConstraint(f"{selected} OR {other}", name="ck_run_slots_selected"),
        sa.UniqueConstraint("pipeline_run_id", "slot", name="uq_run_slots_run_slot"),
        sa.UniqueConstraint(
            "pipeline_run_id", "position", name="uq_run_slots_run_position"
        ),
    )

    op.create_table(
        "reference_bundles",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("bundle_id", sa.String(64), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("bundle_type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("content_digest", sa.String(71), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("licence_spdx", sa.String(255), nullable=False),
        sa.Column("redistributable", sa.Boolean(), nullable=False),
        sa.Column("citation", sa.Text()),
        sa.Column("metadata", JSONB, nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("verification_status", sa.String(16), nullable=False),
        sa.Column("registered_at", TS, nullable=False),
        sa.Column("registered_by_user_id", UUID),
        sa.Column("last_full_verification_at", TS, nullable=False),
        sa.Column("withdrawn_at", TS),
        _fk("reference_bundles", "registered_by_user_id", "users"),
        _enum("reference_bundles", "status", ("active", "withdrawn")),
        _enum("reference_bundles", "verification_status", ("verified", "failed")),
        sa.UniqueConstraint(
            "bundle_id", "version", name="uq_reference_bundles_id_version"
        ),
    )
    op.create_index(
        "ix_reference_bundles_type", "reference_bundles", ["bundle_type", "status"]
    )

    op.create_table(
        "run_slot_bundles",
        sa.Column("run_slot_id", UUID, primary_key=True),
        sa.Column("requirement_name", sa.String(64), primary_key=True),
        sa.Column("reference_bundle_id", UUID, nullable=False),
        _fk("run_slot_bundles", "run_slot_id", "run_slots", ondelete="CASCADE"),
        _fk("run_slot_bundles", "reference_bundle_id", "reference_bundles"),
    )
    op.create_index(
        "ix_run_slot_bundles_reference_bundle_id",
        "run_slot_bundles",
        ["reference_bundle_id"],
    )

    op.create_table(
        "step_runs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("pipeline_run_id", UUID, nullable=False),
        sa.Column("run_slot_id", UUID, nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("outcome", sa.String(24)),
        sa.Column("executor", sa.String(16), nullable=False),
        sa.Column("runtime", sa.String(16), nullable=False),
        sa.Column("image", sa.String(300)),
        sa.Column("sif_sha256", sa.String(64)),
        sa.Column("external_job_id", sa.String(128)),
        sa.Column("executor_state", JSONB),
        sa.Column("queued_at", TS, nullable=False),
        sa.Column("started_at", TS),
        sa.Column("finished_at", TS),
        sa.Column("exit_code", sa.Integer()),
        sa.Column("granted_cpus", sa.Integer()),
        sa.Column("granted_memory_mb", sa.Integer()),
        sa.Column("granted_gpus", sa.Integer()),
        sa.Column("granted_wall_time_minutes", sa.Integer()),
        sa.Column("usage_wall_seconds", sa.Numeric(14, 3)),
        sa.Column("usage_cpu_core_hours", sa.Numeric(14, 4)),
        sa.Column("usage_gpu_hours", sa.Numeric(14, 4)),
        sa.Column("usage_max_rss_mb", sa.Integer()),
        sa.Column("step_provenance", JSONB),
        sa.Column("step_provenance_sha256", sa.String(64)),
        sa.Column("step_error", JSONB),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("failure_message", sa.Text()),
        sa.Column("log_path", sa.Text()),
        sa.Column("log_truncated", sa.Boolean(), nullable=False, server_default=FALSE),
        sa.Column("orchestrator_task_id", sa.String(255)),
        sa.Column("cancel_requested_at", TS),
        sa.Column("legacy_job_id", UUID),
        _fk("step_runs", "pipeline_run_id", "pipeline_runs", ondelete="CASCADE"),
        _fk("step_runs", "run_slot_id", "run_slots", ondelete="CASCADE"),
        _fk("step_runs", "legacy_job_id", "processing_jobs"),
        _enum("step_runs", "trigger", ("initial", "auto_retry", "user_resume")),
        _enum("step_runs", "status", STATUSES),
        _enum(
            "step_runs",
            "outcome",
            (
                "succeeded",
                "rejected",
                "failed",
                "canceled",
                "timed_out",
                "contract_violation",
            ),
        ),
        _enum("step_runs", "executor", ("subprocess", "docker", "slurm", "external")),
        _enum("step_runs", "runtime", ("docker", "apptainer", "none")),
        sa.CheckConstraint("attempt >= 1", name="ck_step_runs_attempt"),
        sa.CheckConstraint(
            "(status IN ('queued', 'running') AND outcome IS NULL)"
            " OR (status = 'awaiting_review' AND outcome IS NOT NULL AND outcome = 'succeeded')"
            " OR (status = 'succeeded' AND outcome IS NOT NULL AND outcome = 'succeeded')"
            " OR (status = 'failed' AND outcome IS NOT NULL AND outcome IN"
            " ('rejected', 'failed', 'timed_out', 'contract_violation'))"
            " OR (status = 'canceled' AND outcome IS NOT NULL AND outcome = 'canceled')",
            name="ck_step_runs_status_outcome",
        ),
        sa.CheckConstraint(
            f"(status IN ({_in(TERMINAL)})) = (finished_at IS NOT NULL)",
            name="ck_step_runs_finished_at",
        ),
        sa.CheckConstraint(
            "executor NOT IN ('docker', 'slurm') OR image IS NOT NULL",
            name="ck_step_runs_image",
        ),
        sa.CheckConstraint(
            "executor IN ('subprocess', 'external') OR (granted_cpus IS NOT NULL"
            " AND granted_memory_mb IS NOT NULL AND granted_gpus IS NOT NULL"
            " AND granted_wall_time_minutes IS NOT NULL)",
            name="ck_step_runs_granted_resources",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome <> 'rejected' OR step_error IS NOT NULL",
            name="ck_step_runs_step_error",
        ),
        sa.UniqueConstraint("run_slot_id", "attempt", name="uq_step_runs_slot_attempt"),
        sa.UniqueConstraint("legacy_job_id", name="uq_step_runs_legacy_job_id"),
    )
    _partial_unique(
        "uq_step_runs_one_active", "step_runs", ["run_slot_id"], f"status IN ({_in(ACTIVE)})"
    )
    _partial_unique(
        "uq_step_runs_one_success", "step_runs", ["run_slot_id"], "status = 'succeeded'"
    )
    op.create_index("ix_step_runs_pipeline_run_id", "step_runs", ["pipeline_run_id"])
    _partial_index(
        "ix_step_runs_active", "step_runs", ["status"], "status IN ('queued', 'running')"
    )
    _partial_index(
        "ix_step_runs_external_job",
        "step_runs",
        ["executor", "external_job_id"],
        "external_job_id IS NOT NULL",
    )
    # The two tables reference each other.
    op.create_foreign_key(
        "fk_pipeline_runs_failed_step_run_id_step_runs",
        "pipeline_runs",
        "step_runs",
        ["failed_step_run_id"],
        ["id"],
    )

    op.create_table(
        "artifacts",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("pipeline_run_id", UUID, nullable=False),
        sa.Column("step_run_id", UUID),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("port", sa.String(64), nullable=False),
        sa.Column("artefact_type", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("supersedes_artifact_id", UUID),
        sa.Column("created_at", TS, nullable=False),
        _fk("artifacts", "case_id", "cases"),
        _fk("artifacts", "pipeline_run_id", "pipeline_runs", ondelete="CASCADE"),
        _fk("artifacts", "step_run_id", "step_runs", ondelete="CASCADE"),
        _fk("artifacts", "supersedes_artifact_id", "artifacts"),
        _enum("artifacts", "origin", ("step", "review", "legacy")),
        sa.CheckConstraint(
            "origin = 'legacy' OR sha256 IS NOT NULL", name="ck_artifacts_sha256_required"
        ),
        sa.CheckConstraint(
            "origin = 'legacy' OR size_bytes IS NOT NULL", name="ck_artifacts_size_required"
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0", name="ck_artifacts_size_bytes"
        ),
        sa.CheckConstraint(
            "origin = 'review' OR step_run_id IS NOT NULL",
            name="ck_artifacts_step_run_required",
        ),
    )
    _partial_unique(
        "uq_artifacts_step_port",
        "artifacts",
        ["step_run_id", "port"],
        "origin <> 'review'",
    )
    op.create_index("ix_artifacts_case_id", "artifacts", ["case_id"])
    op.create_index(
        "ix_artifacts_run_type", "artifacts", ["pipeline_run_id", "artefact_type"]
    )

    op.create_table(
        "reports",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("case_id", UUID, nullable=False),
        sa.Column("pipeline_run_id", UUID, nullable=False),
        sa.Column("step_run_id", UUID, nullable=False),
        sa.Column("pdf_artifact_id", UUID, nullable=False),
        sa.Column("json_artifact_id", UUID),
        sa.Column("template", sa.String(64), nullable=False),
        sa.Column("language", sa.String(8), nullable=False),
        sa.Column("content_sha256", sa.String(64)),
        sa.Column("created_at", TS, nullable=False),
        _fk("reports", "case_id", "cases"),
        _fk("reports", "pipeline_run_id", "pipeline_runs"),
        _fk("reports", "step_run_id", "step_runs"),
        _fk("reports", "pdf_artifact_id", "artifacts"),
        _fk("reports", "json_artifact_id", "artifacts"),
        sa.CheckConstraint(
            "template = 'legacy-technical' OR json_artifact_id IS NOT NULL",
            name="ck_reports_json_artifact_required",
        ),
        sa.CheckConstraint(
            "template = 'legacy-technical' OR content_sha256 IS NOT NULL",
            name="ck_reports_content_sha256_required",
        ),
        sa.UniqueConstraint(
            "pipeline_run_id", "template", "language", name="uq_reports_run_template_language"
        ),
    )
    op.create_index("ix_reports_case_id", "reports", ["case_id"])

    op.create_table(
        "slot_implementation_settings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("pipeline_id", sa.String(64), nullable=False),
        sa.Column("slot", sa.String(64), nullable=False),
        sa.Column("implementation_id", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=FALSE),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("updated_by_user_id", UUID),
        _fk("slot_implementation_settings", "updated_by_user_id", "users"),
        sa.CheckConstraint(
            "NOT is_default OR enabled", name="ck_slot_impl_settings_default_enabled"
        ),
        sa.UniqueConstraint(
            "pipeline_id", "slot", "implementation_id", name="uq_slot_impl_settings"
        ),
    )
    _partial_unique(
        "uq_slot_impl_settings_default",
        "slot_implementation_settings",
        ["pipeline_id", "slot"],
        "is_default",
    )


def _alter_existing_tables() -> None:
    op.create_check_constraint("ck_users_role", "users", f"role IN ({_in(ROLES)})")

    for table in ("audit_events", "notifications"):
        op.add_column(table, sa.Column("case_id", UUID))
        op.add_column(table, sa.Column("pipeline_run_id", UUID))
        op.create_foreign_key(
            f"fk_{table}_case_id_cases", table, "cases", ["case_id"], ["id"]
        )
        op.create_foreign_key(
            f"fk_{table}_pipeline_run_id_pipeline_runs",
            table,
            "pipeline_runs",
            ["pipeline_run_id"],
            ["id"],
        )
        op.create_index(f"ix_{table}_case_id", table, ["case_id"])
        op.create_index(f"ix_{table}_pipeline_run_id", table, ["pipeline_run_id"])

    # share_links.case_id stays nullable in this migration (docs/data-model.md section 6
    # lists it as NOT NULL after the back-fill): the legacy upload endpoint keeps creating
    # studies and share links without a case row until the worker dual-writes (F0-15) and
    # migration 0012 reconciles. Making it NOT NULL here would break legacy endpoints.
    op.add_column("share_links", sa.Column("case_id", UUID))
    op.add_column("share_links", sa.Column("report_id", UUID))
    op.create_foreign_key(
        "fk_share_links_case_id_cases", "share_links", "cases", ["case_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_share_links_report_id_reports", "share_links", "reports", ["report_id"], ["id"]
    )
    op.create_index("ix_share_links_case_id", "share_links", ["case_id"])
    op.create_index("ix_share_links_report_id", "share_links", ["report_id"])


# --- back-fill ---------------------------------------------------------------------------
RUN_STATUS = {
    "uploaded": "queued",
    "queued": "queued",
    "processing": "running",
    "completed": "succeeded",
    "failed": "failed",
    "canceled": "canceled",
}
JOB_OUTCOME = {"succeeded": "succeeded", "failed": "failed", "canceled": "canceled"}
REVIEW_STATUSES = {"technical_only", "reviewed", "validated"}
JSON_COLUMNS = {
    "legacy_attributes",
    "requested_implementations",
    "effective_parameters",
    "executor_state",
}

STUDY_COLUMNS = (
    "id, owner_user_id, original_filename, stored_path, output_path, pdf_path,"
    " output_zip_path, bids_subject_id, processor_backend, container_image, bids_path,"
    " preproc_output_path, rendered_png_dir, processing_warnings, clinical_review_status,"
    " CAST(status AS VARCHAR) AS status, created_at, updated_at, processing_started_at,"
    " processing_finished_at, deleted_at, keep_forever, error_message, processor_name,"
    " processor_version, file_size, checksum"
)
JOB_COLUMNS = (
    "id, study_id, celery_task_id, status, queued_at, started_at, finished_at,"
    " cancel_requested_at, worker_name, exit_code, log_path, error_message"
)


def _utc(value: datetime | None) -> datetime | None:
    """Legacy timestamps are naive UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _relative(value: str | None, root: Path) -> str | None:
    """`value` relative to the storage root, '/'-separated; ValueError if outside it."""
    if not value:
        return None
    path = Path(value)
    for base, candidate in ((root, path), (root.resolve(), path.resolve())):
        try:
            return candidate.relative_to(base).as_posix()
        except ValueError:
            continue
    raise ValueError(value)


def _kebab(value: str | None) -> str:
    """Legacy backend name as a kebab identifier ('unknown' when nothing is left)."""
    text = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")[:55].strip("-")
    return text if re.match(KEBAB, text) else "unknown"


def _jsonable(row: dict) -> dict:
    return {
        key: (json.dumps(val) if key in JSON_COLUMNS and val is not None else val)
        for key, val in row.items()
    }


def _insert(conn, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    columns = list(rows[0])
    values = ", ".join(
        f"CAST(:{c} AS jsonb)" if c in JSON_COLUMNS else f":{c}" for c in columns
    )
    conn.execute(
        sa.text(
            f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({values})"
            " ON CONFLICT DO NOTHING"
        ),
        [_jsonable(r) for r in rows],
    )


def _derive(study_id: uuid.UUID, suffix: str) -> uuid.UUID:
    return uuid.uuid5(LEGACY_NS, f"{study_id}/{suffix}")


def backfill(conn, storage_root: Path) -> dict[str, int]:
    """Mirror legacy studies and jobs into the new tables (docs/data-model.md 10.2, 10.3).

    Idempotent. Raises RuntimeError, writing nothing, if the legacy data cannot be mapped.
    """
    studies = conn.execute(
        sa.text(f"SELECT {STUDY_COLUMNS} FROM studies ORDER BY id")
    ).mappings().all()
    jobs_by_study: dict[uuid.UUID, list] = {}
    for job in conn.execute(
        sa.text(f"SELECT {JOB_COLUMNS} FROM processing_jobs ORDER BY study_id, queued_at, id")
    ).mappings():
        jobs_by_study.setdefault(job["study_id"], []).append(job)

    errors: list[str] = []
    labels: dict[str, uuid.UUID] = {}

    def rel(value, what: str, owner) -> str | None:
        try:
            return _relative(value, storage_root)
        except ValueError:
            errors.append(f"{what} of {owner} is outside the storage root: {value}")
            return None

    plan = []
    for study in studies:
        sid = study["id"]
        jobs = jobs_by_study.get(sid, [])
        label = sid.hex[:12]
        if labels.setdefault(label, sid) != sid:
            errors.append(f"subject label collision between {labels[label]} and {sid}")
        if study["status"] not in RUN_STATUS:
            errors.append(f"study {sid} has unknown status {study['status']!r}")
        if study["clinical_review_status"] not in REVIEW_STATUSES:
            errors.append(
                f"study {sid} has unknown clinical_review_status"
                f" {study['clinical_review_status']!r}"
            )
        for job in jobs:
            if job["status"] not in RUN_STATUS:
                errors.append(f"job {job['id']} has unknown status {job['status']!r}")
        if sum(1 for j in jobs if j["status"] == "completed") > 1:
            errors.append(f"study {sid} has more than one completed job")
        paths = {
            name: rel(study[name], name, f"study {sid}")
            for name in (
                "stored_path",
                "output_path",
                "pdf_path",
                "output_zip_path",
                "bids_path",
                "preproc_output_path",
                "rendered_png_dir",
            )
        }
        if not paths["stored_path"]:
            errors.append(f"study {sid} has an empty stored_path")
        log_paths = {j["id"]: rel(j["log_path"], "log_path", f"job {j['id']}") for j in jobs}
        plan.append((study, jobs, paths, log_paths))
    if errors:
        raise RuntimeError(
            "legacy data cannot be mapped to the new model; fix it and re-run:\n  "
            + "\n  ".join(errors)
        )

    rows: dict[str, list[dict]] = {
        t: []
        for t in (
            "cases",
            "input_files",
            "pipeline_runs",
            "run_inputs",
            "run_slots",
            "step_runs",
            "artifacts",
            "reports",
        )
    }
    for study, jobs, paths, log_paths in plan:
        _map_study(study, jobs, paths, log_paths, rows)

    for table in rows:  # parents first (dict order)
        _insert(conn, table, rows[table])
    for table in ("audit_events", "notifications", "share_links"):
        conn.execute(
            sa.text(
                f"UPDATE {table} SET case_id = study_id WHERE study_id IS NOT NULL"
                " AND case_id IS NULL AND study_id IN (SELECT id FROM cases)"
            )
        )
    return {table: len(r) for table, r in rows.items()}


def _map_study(study, jobs, paths, log_paths, rows) -> None:
    sid = study["id"]
    owner = study["owner_user_id"]
    created = _utc(study["created_at"])
    updated = _utc(study["updated_at"])
    status = RUN_STATUS[study["status"]]
    terminal = status in TERMINAL
    backend = study["processor_backend"]
    stub = backend == "dummy"
    latest = jobs[-1] if jobs else None

    rows["cases"].append(
        {
            "id": sid,
            "owner_user_id": owner,
            "subject_label": sid.hex[:12],
            "session_label": "acute",
            "sex": None,
            "age_band": None,
            "deployment_profile": "legacy",
            "created_at": created,
            "updated_at": updated,
            "deleted_at": _utc(study["deleted_at"]),
            "keep_forever": study["keep_forever"],
            "legacy_study_id": sid,
            "legacy_attributes": {
                "original_filename": study["original_filename"],
                "bids_subject_id": study["bids_subject_id"],
                "processor_backend": backend,
                "processor_name": study["processor_name"],
                "processor_version": study["processor_version"],
                "container_image": study["container_image"],
                "processing_warnings": study["processing_warnings"],
                "bids_path": paths["bids_path"],
                "preproc_output_path": paths["preproc_output_path"],
                "rendered_png_dir": paths["rendered_png_dir"],
                "output_path": paths["output_path"],
            },
        }
    )

    input_id = _derive(sid, "input")
    checksum = (study["checksum"] or "").lower()
    size = study["file_size"]
    rows["input_files"].append(
        {
            "id": input_id,
            "case_id": sid,
            "role": LEGACY_UPLOAD,
            "artefact_type": LEGACY_UPLOAD,
            "storage_path": paths["stored_path"],
            "sha256": checksum if re.match(SHA256_HEX, checksum) else None,
            "size_bytes": size if size is not None and size >= 0 else None,
            "uploaded_by_user_id": owner,
            "created_at": created,
        }
    )

    failed_like = status in ("failed", "canceled")
    rows["pipeline_runs"].append(
        {
            "id": sid,
            "case_id": sid,
            "created_by_user_id": owner,
            "pipeline_id": "legacy",
            "pipeline_version": "0.0.0",
            "pipeline_sha256": None,
            "platform_version": "legacy",
            "artefact_types_version": "legacy",
            "input_set": "legacy-single-file",
            "seed": 0,
            "status": status,
            "requested_implementations": {},
            "accounting_submitted_by": f"user:{owner}",
            "accounting_submit_as": "service-account",
            "accounting_billing_account": None,
            "created_at": created,
            "started_at": _utc(study["processing_started_at"]),
            "finished_at": (
                _utc(study["processing_finished_at"]) or updated if terminal else None
            ),
            "updated_at": updated,
            "cancel_requested_at": _utc(latest["cancel_requested_at"]) if latest else None,
            "resume_count": max(len(jobs) - 1, 0),
            "failure_code": "legacy_failure" if status == "failed" else None,
            "failure_message": study["error_message"] if failed_like else None,
            "legacy_clinical_review_status": study["clinical_review_status"],
        }
    )
    rows["run_inputs"].append(
        {"pipeline_run_id": sid, "role": LEGACY_UPLOAD, "input_file_id": input_id}
    )

    impl = _kebab(backend)
    slot_id = _derive(sid, "slot/legacy")
    rows["run_slots"].append(
        {
            "id": slot_id,
            "pipeline_run_id": sid,
            "slot": "legacy",
            "position": 0,
            "optional": False,
            "status": "selected",
            "reason": None,
            "implementation_id": impl,
            "step_name": f"legacy-{impl}",
            "step_version": "0.0.0",
            "image": None,
            "stub": stub,
            "chosen_by": "deployment_default",
            "effective_parameters": {},
        }
    )

    for index, job in enumerate(jobs):
        job_status = RUN_STATUS[job["status"]]
        stale = job_status in ("queued", "running") and job is not latest
        failure_code = None
        if stale:
            job_status, failure_code = "failed", "legacy_stale_job"
        job_terminal = job_status in TERMINAL
        queued = _utc(job["queued_at"])
        started = _utc(job["started_at"])
        finished = None
        if job_terminal:
            finished = _utc(job["finished_at"])
            if finished is None and stale:
                finished = _utc(jobs[index + 1]["queued_at"])
            elif finished is None:
                finished = started or queued
        worker = job["worker_name"]
        rows["step_runs"].append(
            {
                "id": job["id"],
                "pipeline_run_id": sid,
                "run_slot_id": slot_id,
                "attempt": index + 1,
                "trigger": "initial" if index == 0 else "user_resume",
                "status": job_status,
                "outcome": JOB_OUTCOME.get(job_status),
                "executor": "subprocess",
                "runtime": "none",
                "executor_state": {"worker_name": worker} if worker else None,
                "queued_at": queued,
                "started_at": started,
                "finished_at": finished,
                "exit_code": job["exit_code"],
                "failure_code": failure_code,
                "failure_message": job["error_message"],
                "log_path": log_paths[job["id"]],
                "orchestrator_task_id": job["celery_task_id"],
                "cancel_requested_at": _utc(job["cancel_requested_at"]),
                "legacy_job_id": job["id"],
            }
        )

    # Artefacts and the report hang on the latest completed job, else on the latest job.
    target = next((j for j in reversed(jobs) if j["status"] == "completed"), latest)
    if target is None:
        if paths["pdf_path"] or paths["output_zip_path"]:
            log.warning("study %s has output files but no job; no artefact created", sid)
        return
    for key, port, art_type in (
        ("pdf_path", "technical_report", "legacy_technical_report_pdf"),
        ("output_zip_path", "outputs_zip", "legacy_outputs_zip"),
    ):
        if not paths[key]:
            continue
        artifact_id = _derive(sid, f"artifact/{port}")
        rows["artifacts"].append(
            {
                "id": artifact_id,
                "case_id": sid,
                "pipeline_run_id": sid,
                "step_run_id": target["id"],
                "origin": "legacy",
                "port": port,
                "artefact_type": art_type,
                "storage_path": paths[key],
                "sha256": None,
                "size_bytes": None,
                "synthetic": stub,
                "created_at": updated,
            }
        )
        if key == "pdf_path":
            rows["reports"].append(
                {
                    "id": _derive(sid, "report"),
                    "case_id": sid,
                    "pipeline_run_id": sid,
                    "step_run_id": target["id"],
                    "pdf_artifact_id": artifact_id,
                    "json_artifact_id": None,
                    "template": "legacy-technical",
                    "language": "es",
                    "content_sha256": None,
                    "created_at": updated,
                }
            )


# --- downgrade ---------------------------------------------------------------------------
def downgrade() -> None:
    conn = op.get_bind()
    allow = context.get_x_argument(as_dictionary=True).get("allow_data_loss") == "true"
    if not allow:
        problems = []
        checks = {
            "cases created by new code (no legacy study)": (
                "SELECT count(*) FROM cases WHERE legacy_study_id IS NULL"
            ),
            "runs of non-legacy pipelines": (
                "SELECT count(*) FROM pipeline_runs WHERE pipeline_id <> 'legacy'"
            ),
            "reference bundles": "SELECT count(*) FROM reference_bundles",
            "slot implementation settings": (
                "SELECT count(*) FROM slot_implementation_settings"
            ),
        }
        for label, query in checks.items():
            count = conn.execute(sa.text(query)).scalar()
            if count:
                problems.append(f"{count} {label}")
        if problems:
            raise RuntimeError(
                "downgrading 0011 would delete data that the legacy tables do not hold ("
                + "; ".join(problems)
                + "). Re-run with `alembic -x allow_data_loss=true downgrade ...` to"
                " accept the loss."
            )

    for table in ("share_links", "notifications", "audit_events"):
        op.drop_index(f"ix_{table}_case_id", table_name=table)
        if table == "share_links":
            op.drop_index("ix_share_links_report_id", table_name=table)
            op.drop_constraint("fk_share_links_report_id_reports", table, type_="foreignkey")
            op.drop_column(table, "report_id")
        else:
            op.drop_index(f"ix_{table}_pipeline_run_id", table_name=table)
            op.drop_constraint(
                f"fk_{table}_pipeline_run_id_pipeline_runs", table, type_="foreignkey"
            )
            op.drop_column(table, "pipeline_run_id")
        op.drop_constraint(f"fk_{table}_case_id_cases", table, type_="foreignkey")
        op.drop_column(table, "case_id")
    op.drop_constraint("ck_users_role", "users", type_="check")

    op.drop_table("slot_implementation_settings")
    op.drop_table("reports")
    op.drop_table("artifacts")
    op.drop_constraint(
        "fk_pipeline_runs_failed_step_run_id_step_runs", "pipeline_runs", type_="foreignkey"
    )
    op.drop_table("step_runs")
    op.drop_table("run_slot_bundles")
    op.drop_table("reference_bundles")
    op.drop_table("run_slots")
    op.drop_table("run_inputs")
    op.drop_table("pipeline_runs")
    op.drop_table("input_files")
    op.drop_table("cases")
