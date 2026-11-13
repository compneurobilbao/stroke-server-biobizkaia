"""explicit cancellation flag on processing jobs

Revision ID: 0010_job_cancel_requested_at
Revises: 0009_retention_policy
Create Date: 2026-11-13
"""

from alembic import op
import sqlalchemy as sa

revision = "0010_job_cancel_requested_at"
down_revision = "0009_retention_policy"
branch_labels = None
depends_on = None

LEGACY_PREFIX = "Cancelación solicitada por el usuario"


def upgrade() -> None:
    op.add_column(
        "processing_jobs",
        sa.Column("cancel_requested_at", sa.DateTime(), nullable=True),
    )
    # One-time back-fill for jobs still running when the flag was introduced.
    jobs = sa.table(
        "processing_jobs",
        sa.column("status", sa.String),
        sa.column("error_message", sa.Text),
        sa.column("cancel_requested_at", sa.DateTime),
    )
    op.execute(
        jobs.update()
        .where(
            jobs.c.status == "processing",
            jobs.c.error_message.like(LEGACY_PREFIX + "%"),
        )
        .values(cancel_requested_at=sa.func.now())
    )


def downgrade() -> None:
    op.drop_column("processing_jobs", "cancel_requested_at")
