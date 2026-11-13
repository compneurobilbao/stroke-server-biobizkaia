"""Shared building blocks of the Phase 0 data-model tables (docs/data-model.md section 2)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import JSON, CheckConstraint, DateTime
from sqlalchemy.dialects import postgresql

# jsonb on PostgreSQL; JSON elsewhere. Python None is stored as SQL NULL, never as JSON null.
JSONType = JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)


def tz_datetime() -> DateTime:
    return DateTime(timezone=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def sql_list(values: Iterable[str]) -> str:
    return ", ".join("'" + v + "'" for v in values)


def enum_check(table: str, column: str, values: Iterable[str]) -> CheckConstraint:
    """`CHECK (column IN (...))` named ck_<table>_<column> (a varchar enumeration)."""
    return CheckConstraint(
        f"{column} IN ({sql_list(values)})", name=f"ck_{table}_{column}"
    )
