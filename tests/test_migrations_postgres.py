"""Alembic migrations against a real PostgreSQL database.

Skipped unless TEST_POSTGRES_URL is set (the `migrations-postgres` CI job sets it). The
tests reset the `public` schema of that database, so point it at a scratch database.
Add further PostgreSQL migration tests here and mark them with `pytest.mark.postgres`.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("TEST_POSTGRES_URL"),
        reason="TEST_POSTGRES_URL is not set",
    ),
]

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
EXPECTED_TABLES = {"users", "studies", "processing_jobs", "notifications", "audit_events"}


def alembic(*args: str) -> None:
    env = {**os.environ, "DATABASE_URL": os.environ["TEST_POSTGRES_URL"]}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"


@pytest.fixture()
def pg_engine():
    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def user_tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def test_upgrade_downgrade_upgrade(pg_engine):
    alembic("upgrade", "head")
    assert EXPECTED_TABLES <= user_tables(pg_engine)

    alembic("downgrade", "base")
    assert user_tables(pg_engine) == set()

    alembic("upgrade", "head")
    assert EXPECTED_TABLES <= user_tables(pg_engine)


def test_single_head(pg_engine):
    alembic("upgrade", "head")
    out = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": os.environ["TEST_POSTGRES_URL"]},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert len(out.strip().splitlines()) == 1, out
