import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("STORAGE_ROOT", "./test-data/studies")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from app.core.config import get_settings
from app.db.session import get_db
from app.main import app
from tests import pgdb


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "postgres: needs a PostgreSQL database; skipped unless TEST_POSTGRES_URL is set",
    )


@pytest.fixture(autouse=True)
def _drop_postgres_databases():
    """With TEST_DB=postgres every test gets its own database; drop it afterwards."""
    yield
    if pgdb.enabled():
        pgdb.cleanup_test_databases()


def pytest_sessionfinish(session, exitstatus):
    if pgdb.enabled():
        pgdb.cleanup_template()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    yield from _make_client(tmp_path, monkeypatch)


@pytest.fixture()
def compneuro_client(tmp_path, monkeypatch):
    yield from _make_client(
        tmp_path,
        monkeypatch,
        PROCESSOR_BACKEND="compneuro",
        ALLOWED_EXTENSIONS=".nii.gz",
    )


def _make_client(tmp_path, monkeypatch, **extra_env):
    storage_path = tmp_path / "studies"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_path))
    monkeypatch.setenv("PROCESSOR_BACKEND", "dummy")
    monkeypatch.setenv(
        "ALLOWED_EXTENSIONS", ".nii,.nii.gz,.dcm,.zip,.tar,.tar.gz,.gz,.json,.txt"
    )
    for key, value in extra_env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    engine = pgdb.new_engine(
        tmp_path, "test.db", connect_args={"check_same_thread": False}
    )
    monkeypatch.setenv("DATABASE_URL", pgdb.database_url(engine))
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    class DummyTask:
        @staticmethod
        def delay(*args, **kwargs):
            return None

    import app.api.studies as routes

    routes.process_study = DummyTask
    with TestClient(app) as test_client:
        test_client.app.state.testing_session_local = TestingSessionLocal
        yield test_client
    app.dependency_overrides.clear()
    get_settings.cache_clear()
