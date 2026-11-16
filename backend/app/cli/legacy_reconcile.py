"""Dry run of the legacy reconciliation of migration 0012 (docs/data-model.md 10.1, 10.3).

Run it before ``alembic upgrade`` to 0012, against the database to be migrated (at
revision 0011)::

    python -m app.cli.legacy_reconcile

It prints, per table, how many rows the reconciliation would insert, update or leave
unchanged and how many attempt sets it would rewrite; it never writes (the work happens
inside a transaction that is rolled back). When legacy rows cannot be mapped it prints
the affected ids and exits with status 1: fix them and run it again. Running it after
the cut-over only describes how the frozen legacy tables differ from the live data; the
migration itself never re-runs the reconciliation.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

from app.core.config import get_settings

MIGRATION = (
    Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0012_legacy_cutover.py"
)
EXPECTED_REVISION = "0011_case_model"


def _migration():
    spec = importlib.util.spec_from_file_location("_migration_0012_legacy_cutover", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    settings = get_settings()
    parser = argparse.ArgumentParser(
        description="Report what the legacy reconciliation of migration 0012 would write"
    )
    parser.add_argument(
        "--storage-root",
        default=str(settings.storage_root),
        help="Storage root used to make legacy paths relative (default: STORAGE_ROOT)",
    )
    args = parser.parse_args(argv)
    module = _migration()
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            revision = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            transaction = conn.begin()
            try:
                report = module.reconcile(conn, Path(args.storage_root), dry_run=True)
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
            finally:
                transaction.rollback()
    finally:
        engine.dispose()
    report["database_revision"] = revision
    print(json.dumps(report, indent=2, sort_keys=True))
    if revision != EXPECTED_REVISION:
        print(
            f"warning: the database is at revision {revision}, not {EXPECTED_REVISION};"
            " the report does not describe a pending migration",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
