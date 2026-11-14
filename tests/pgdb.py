"""Test databases for the suites that run on SQLite (fast path) and on PostgreSQL (CI).

``TEST_DB=postgres`` (plus ``TEST_POSTGRES_URL``, a server where the user may create
databases) switches every ``new_engine`` call from a per-test SQLite file built with
``create_all`` to a per-test PostgreSQL database cloned from a template. The template is
built once per pytest process by running ``alembic upgrade head`` on an empty database, so
the schema is the production one: foreign keys, check constraints, partial unique indexes
and real ``SELECT ... FOR UPDATE`` are all enforced. Every test gets its own database
(``CREATE DATABASE ... TEMPLATE``), so commits made by the code under test never leak
between tests and concurrent sessions can be used freely. ``tests/conftest.py`` drops the
databases after each test and the template at the end of the session.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Engine, make_url

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"

_template: str | None = None
_databases: list[str] = []
_engines: list[Engine] = []


def enabled() -> bool:
    return os.environ.get("TEST_DB", "").lower() == "postgres"


def _base_url() -> URL:
    raw = os.environ.get("TEST_POSTGRES_URL")
    if not raw:
        raise RuntimeError("TEST_DB=postgres needs TEST_POSTGRES_URL")
    return make_url(raw)


def _admin_engine() -> Engine:
    return create_engine(_base_url(), isolation_level="AUTOCOMMIT")


def _url_text(url: URL) -> str:
    return url.render_as_string(hide_password=False)


def _create_database(name: str, template: str | None = None) -> None:
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            suffix = f' TEMPLATE "{template}"' if template else ""
            conn.execute(text(f'CREATE DATABASE "{name}"{suffix}'))
    finally:
        admin.dispose()


def _drop_database(name: str) -> None:
    admin = _admin_engine()
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


def _ensure_template() -> str:
    global _template
    if _template is None:
        name = f"stroke_tpl_{uuid.uuid4().hex[:10]}"
        _create_database(name)
        _template = name
        env = {
            **os.environ,
            "DATABASE_URL": _url_text(_base_url().set(database=name)),
        }
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=BACKEND_DIR,
            env=env,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}"
            )
    return _template


def new_engine(tmp_path: Path, filename: str = "test.db", **kwargs) -> Engine:
    """A fresh, empty (schema only) database for one test.

    SQLite: a file under ``tmp_path`` built from the models. PostgreSQL: a clone of the
    migrated template. ``kwargs`` are passed to ``create_engine`` (SQLite connect args
    are ignored on PostgreSQL).
    """
    if not enabled():
        import app.models  # noqa: F401  (registers every table)
        from app.db.base import Base

        engine = create_engine(f"sqlite:///{tmp_path / filename}", **kwargs)
        Base.metadata.create_all(engine)
        return engine
    template = _ensure_template()
    name = f"stroke_t_{uuid.uuid4().hex[:12]}"
    _create_database(name, template)
    _databases.append(name)
    kwargs.pop("connect_args", None)
    engine = create_engine(_base_url().set(database=name), pool_pre_ping=True, **kwargs)
    _engines.append(engine)
    return engine


def database_url(engine: Engine) -> str:
    return _url_text(engine.url)


def cleanup_test_databases() -> None:
    """Dispose the engines created by the test and drop their databases."""
    while _engines:
        _engines.pop().dispose()
    while _databases:
        _drop_database(_databases.pop())


def cleanup_template() -> None:
    global _template
    cleanup_test_databases()
    if _template is not None:
        _drop_database(_template)
        _template = None
