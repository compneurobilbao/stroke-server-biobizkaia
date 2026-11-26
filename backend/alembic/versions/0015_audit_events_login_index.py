"""index for the sign-in throttle on audit_events (card SEC-01)

The login throttle (app/services/auth.py) counts recent ``login_failed`` events of one
e-mail address on every login. This adds ``ix_audit_events_event_type_timestamp`` on
``audit_events (event_type, timestamp)`` so that count does not scan the whole table.
Additive only; downgrade drops the index. ``CREATE INDEX`` blocks writes to
``audit_events`` while it builds (seconds on a table of that size).

Numbering: 0014 is reserved for the removal of the legacy tables (docs/data-model.md,
ADR-0016) and is left unused here. Alembic orders revisions by ``down_revision`` only, so
the gap is harmless: this revision follows 0013, and the future 0014 must name this one
(``0015_audit_events_login_index``) as its ``down_revision``.

Revision ID: 0015_audit_events_login_index
Revises: 0013_run_plans
Create Date: 2026-11-26
"""

from __future__ import annotations

from alembic import op

revision = "0015_audit_events_login_index"
down_revision = "0013_run_plans"
branch_labels = None
depends_on = None

INDEX = "ix_audit_events_event_type_timestamp"


def upgrade() -> None:
    op.create_index(INDEX, "audit_events", ["event_type", "timestamp"])


def downgrade() -> None:
    op.drop_index(INDEX, table_name="audit_events")
