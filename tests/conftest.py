import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")
os.environ.setdefault("STORAGE_ROOT", "./test-data/studies")
os.environ.setdefault("CELERY_BROKER_URL", "memory://")
os.environ.setdefault("CELERY_RESULT_BACKEND", "cache+memory://")

from app.core.config import get_settings
from app.db.base import Base
from app.db.session import get_db
from app.main import app


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "postgres: needs a PostgreSQL database; skipped unless TEST_POSTGRES_URL is set",
    )


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
    db_path = tmp_path / "test.db"
    storage_path = tmp_path / "studies"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("STORAGE_ROOT", str(storage_path))
    monkeypatch.setenv("PROCESSOR_BACKEND", "dummy")
    monkeypatch.setenv(
        "ALLOWED_EXTENSIONS", ".nii,.nii.gz,.dcm,.zip,.tar,.tar.gz,.gz,.json,.txt"
    )
    for key, value in extra_env.items():
        monkeypatch.setenv(key, value)
    get_settings.cache_clear()

    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    TestingSessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    Base.metadata.create_all(bind=engine)

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
