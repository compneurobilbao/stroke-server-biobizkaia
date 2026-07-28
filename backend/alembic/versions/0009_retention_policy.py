"""retention policy

Revision ID: 0009_retention_policy
Revises: 0008_admin_review_quota
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa

revision = "0009_retention_policy"
down_revision = "0008_admin_review_quota"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "studies",
        sa.Column(
            "keep_forever",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.alter_column("studies", "keep_forever", server_default=None)


def downgrade() -> None:
    op.drop_column("studies", "keep_forever")
