"""run plans and preset settings (docs/design/run-plans.md 3.7, ADR-0018, card F1-18)

One additive revision; no table is rewritten and no existing row changes meaning.

Upgrade:

* ``pipeline_runs``: ``plan_mode`` (default ``full``), ``preset_id``, ``preset_version``,
  ``preset_sha256`` and ``targets`` (default ``{}``). Every existing run is a full-plan run
  (rule R0 of the design), so the defaults are the back-fill.
* ``run_slots``: ``plan_role`` and ``upstream``; the ``reason`` check gains
  ``not_requested`` and the ``chosen_by`` check gains ``preset``. ``plan_role`` of existing
  rows of pipeline runs is back-filled (non-optional applicable slots ``target``, optional
  or not selected applicable slots ``if_available``); legacy rows and rows with reason
  ``condition_false`` keep NULL.
* ``input_files.origin`` (declared origin of a supplied input).
* ``preset_settings`` (new), same shape as ``slot_implementation_settings``.

Downgrade drops the new columns and the table and restores the two check lists. It refuses,
with a message, when rows use ``not_requested``, ``chosen_by = 'preset'`` or
``plan_mode <> 'full'``, or when ``run_slots.upstream`` or ``input_files.origin`` hold
data (external-tool provenance and declared origins are not dropped silently), rather than
deleting runs or rewriting their meaning. Preset settings are dropped; their count is logged.

Revision ID: 0013_run_plans
Revises: 0012_legacy_cutover
Create Date: 2026-11-17
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_run_plans"
down_revision = "0012_legacy_cutover"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

UUID = sa.Uuid()
JSONB = postgresql.JSONB(none_as_null=True)
TS = sa.DateTime(timezone=True)

# Copies of app.models.formats (a migration must not change when that module does).
SHA256_HEX = r"^[0-9a-f]{64}$"
KEBAB = r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$"
SEMVER = (
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-[0-9A-Za-z-]+(\.[0-9A-Za-z-]+)*)?$"
)
# (table, column, pattern): PostgreSQL-only regex CHECKs named ck_<table>_<column>_format.
FORMAT_CHECKS = [
    ("pipeline_runs", "preset_id", KEBAB),
    ("pipeline_runs", "preset_version", SEMVER),
    ("pipeline_runs", "preset_sha256", SHA256_HEX),
    ("preset_settings", "pipeline_id", KEBAB),
    ("preset_settings", "preset_id", KEBAB),
]

OLD_REASONS = ("condition_false", "upstream_absent", "no_implementation")
NEW_REASONS = OLD_REASONS + ("not_requested",)
OLD_CHOSEN_BY = ("user", "deployment_default", "pipeline_default")
NEW_CHOSEN_BY = OLD_CHOSEN_BY + ("preset",)
PLAN_MODES = ("full", "preset", "free")
PLAN_ROLES = ("target", "upstream", "if_available")


def _in(values) -> str:
    return ", ".join(f"'{v}'" for v in values)


def _enum_sql(column: str, values) -> str:
    return f"{column} IN ({_in(values)})"


def _is_postgresql() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    conn = op.get_bind()

    # --- pipeline_runs: the plan request (existing rows: full plan, no preset) ----------
    op.add_column(
        "pipeline_runs",
        sa.Column("plan_mode", sa.String(8), nullable=False, server_default="full"),
    )
    op.add_column("pipeline_runs", sa.Column("preset_id", sa.String(64)))
    op.add_column("pipeline_runs", sa.Column("preset_version", sa.String(64)))
    op.add_column("pipeline_runs", sa.Column("preset_sha256", sa.String(64)))
    op.add_column(
        "pipeline_runs",
        sa.Column("targets", JSONB, nullable=False, server_default=sa.text("'{}'")),
    )
    op.create_check_constraint(
        "ck_pipeline_runs_plan_mode",
        "pipeline_runs",
        _enum_sql("plan_mode", PLAN_MODES),
    )
    op.create_check_constraint(
        "ck_pipeline_runs_preset_mode",
        "pipeline_runs",
        "(plan_mode = 'preset') = (preset_id IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_pipeline_runs_preset_all_or_none",
        "pipeline_runs",
        "(preset_id IS NULL) = (preset_version IS NULL)"
        " AND (preset_id IS NULL) = (preset_sha256 IS NULL)",
    )

    # --- run_slots: plan role, upstream copy, extended allowed values -------------------
    op.add_column("run_slots", sa.Column("plan_role", sa.String(12)))
    op.add_column("run_slots", sa.Column("upstream", JSONB))
    filled = conn.execute(
        sa.text(
            "UPDATE run_slots AS s SET plan_role = CASE"
            " WHEN s.optional OR s.status <> 'selected' THEN 'if_available'"
            " ELSE 'target' END"
            " FROM pipeline_runs AS r"
            " WHERE r.id = s.pipeline_run_id AND r.pipeline_id <> 'legacy'"
            " AND s.slot <> 'legacy'"
            " AND (s.reason IS NULL OR s.reason <> 'condition_false')"
        )
    ).rowcount
    log.info("run_slots.plan_role back-filled on %s rows", filled)
    op.drop_constraint("ck_run_slots_reason", "run_slots", type_="check")
    op.create_check_constraint(
        "ck_run_slots_reason", "run_slots", _enum_sql("reason", NEW_REASONS)
    )
    op.drop_constraint("ck_run_slots_chosen_by", "run_slots", type_="check")
    op.create_check_constraint(
        "ck_run_slots_chosen_by", "run_slots", _enum_sql("chosen_by", NEW_CHOSEN_BY)
    )
    op.create_check_constraint(
        "ck_run_slots_plan_role", "run_slots", _enum_sql("plan_role", PLAN_ROLES)
    )
    op.create_check_constraint(
        "ck_run_slots_plan_role_reason",
        "run_slots",
        "reason IS NULL OR reason NOT IN ('not_requested', 'condition_false')"
        " OR plan_role IS NULL",
    )

    # --- input_files: declared origin ----------------------------------------------------
    op.add_column("input_files", sa.Column("origin", JSONB))

    # --- preset_settings (new) -----------------------------------------------------------
    op.create_table(
        "preset_settings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("pipeline_id", sa.String(64), nullable=False),
        sa.Column("preset_id", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "is_default", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("updated_by_user_id", UUID),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name="fk_preset_settings_updated_by_user_id_users",
        ),
        sa.CheckConstraint(
            "NOT is_default OR enabled", name="ck_preset_settings_default_enabled"
        ),
        sa.UniqueConstraint("pipeline_id", "preset_id", name="uq_preset_settings"),
    )
    op.create_index(
        "uq_preset_settings_default",
        "preset_settings",
        ["pipeline_id"],
        unique=True,
        postgresql_where=sa.text("is_default"),
        sqlite_where=sa.text("is_default"),
    )

    if _is_postgresql():
        for table, column, pattern in FORMAT_CHECKS:
            op.create_check_constraint(
                f"ck_{table}_{column}_format", table, f"{column} ~ '{pattern}'"
            )


def downgrade() -> None:
    conn = op.get_bind()
    problems = []
    checks = {
        "run slots with reason not_requested": (
            "SELECT count(*) FROM run_slots WHERE reason = 'not_requested'"
        ),
        "run slots chosen by a preset": (
            "SELECT count(*) FROM run_slots WHERE chosen_by = 'preset'"
        ),
        "runs with a plan mode other than full": (
            "SELECT count(*) FROM pipeline_runs WHERE plan_mode <> 'full'"
        ),
        "run slots with an upstream block": (
            "SELECT count(*) FROM run_slots WHERE upstream IS NOT NULL"
        ),
        "input files with a declared origin": (
            "SELECT count(*) FROM input_files WHERE origin IS NOT NULL"
        ),
    }
    for label, query in checks.items():
        count = conn.execute(sa.text(query)).scalar()
        if count:
            problems.append(f"{count} {label}")
    if problems:
        raise RuntimeError(
            "downgrading 0013 would lose or misread plan data ("
            + "; ".join(problems)
            + "). Runs are not deleted by a downgrade: restore the backup taken before the"
            " upgrade, or remove that data deliberately first."
        )
    settings = conn.execute(sa.text("SELECT count(*) FROM preset_settings")).scalar()
    if settings:
        log.warning("downgrade 0013 drops %s preset settings", settings)

    op.drop_index("uq_preset_settings_default", table_name="preset_settings")
    op.drop_table("preset_settings")

    op.drop_column("input_files", "origin")

    for name in (
        "ck_run_slots_plan_role_reason",
        "ck_run_slots_plan_role",
        "ck_run_slots_chosen_by",
        "ck_run_slots_reason",
    ):
        op.drop_constraint(name, "run_slots", type_="check")
    op.drop_column("run_slots", "upstream")
    op.drop_column("run_slots", "plan_role")
    op.create_check_constraint(
        "ck_run_slots_reason", "run_slots", _enum_sql("reason", OLD_REASONS)
    )
    op.create_check_constraint(
        "ck_run_slots_chosen_by", "run_slots", _enum_sql("chosen_by", OLD_CHOSEN_BY)
    )

    for name in (
        "ck_pipeline_runs_preset_all_or_none",
        "ck_pipeline_runs_preset_mode",
        "ck_pipeline_runs_plan_mode",
    ):
        op.drop_constraint(name, "pipeline_runs", type_="check")
    for column in (
        "targets",
        "preset_sha256",
        "preset_version",
        "preset_id",
        "plan_mode",
    ):
        if column.startswith("preset_") and _is_postgresql():
            op.drop_constraint(
                f"ck_pipeline_runs_{column}_format", "pipeline_runs", type_="check"
            )
        op.drop_column("pipeline_runs", column)
