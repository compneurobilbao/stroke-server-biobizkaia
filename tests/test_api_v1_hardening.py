"""Concurrency and path hardening of the v1 API (review fixes of card F0-30).

Races are simulated sequentially: a hook makes the "other request" happen at the moment
the real code would have been interleaved with it. PostgreSQL ``FOR UPDATE`` itself is
not exercised here (SQLite ignores it); the re-checks under the lock are.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import app.api.v1.cases as cases_module
import app.api.v1.runs as runs_module
from app.models import Artifact, AuditEvent, Case, InputFile, PipelineRun
from orchestrator.resolution import RunRefused
from tests.test_api_v1 import V1Env, nifti_gz


@pytest.fixture()
def v1(client):
    yield V1Env(client)


def _sha(v1, case_id, role="ct"):
    with v1.Session() as db:
        row = db.scalar(
            select(InputFile).where(
                InputFile.case_id == uuid.UUID(case_id), InputFile.role == role
            )
        )
        return row.sha256


# ------------------------------------------------- input change vs run creation


def test_run_created_during_an_upload_refuses_the_change(v1, monkeypatch):
    v1.enable_all()
    case_id = v1.native_case()
    before = _sha(v1, case_id)
    real = cases_module._has_runs
    calls = []

    def has_runs(db, cid):
        calls.append(1)
        if len(calls) == 1:  # the early check passes; then a run appears
            run = v1.start(case_id)
            assert run.status_code == 201
            return False
        return real(db, cid)

    monkeypatch.setattr(cases_module, "_has_runs", has_runs)
    r = v1.upload(case_id, "ct", data=nifti_gz(b"changed during the upload"))
    assert r.status_code == 409 and r.json()["code"] == "case_has_runs"
    assert len(calls) == 2
    # The stored bytes and the row are those the run was created with.
    assert _sha(v1, case_id) == before
    path = next(v1.root.rglob("*_ct.nii.gz"))
    import hashlib

    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_delete_input_after_run_creation_is_refused_under_the_lock(v1):
    v1.enable_all()
    case_id = v1.native_case()
    assert v1.start(case_id).status_code == 201
    r = v1.client.delete(f"/api/v1/cases/{case_id}/inputs/ct", headers=v1.alice)
    assert r.status_code == 409 and r.json()["code"] == "case_has_runs"


def test_new_role_conflict_is_409_and_leaves_no_file(v1, monkeypatch):
    case_id = v1.new_case()
    from sqlalchemy.orm import Session
    from sqlalchemy.exc import IntegrityError

    real_commit = Session.commit

    def failing_commit(self):
        if any(isinstance(o, InputFile) for o in self.new):
            self.rollback()
            raise IntegrityError("insert", {}, Exception("uq_input_files_case_id_role"))
        return real_commit(self)

    monkeypatch.setattr(Session, "commit", failing_commit)
    r = v1.upload(case_id, "ct")
    monkeypatch.undo()
    assert r.status_code == 409 and r.json()["code"] == "input_conflict"
    assert not list(v1.root.rglob("*_ct.nii.gz"))
    assert v1.upload(case_id, "ct").status_code == 201


# ------------------------------------------------ delete vs run creation


def test_run_creation_refuses_a_case_deleted_meanwhile(v1, monkeypatch):
    v1.enable_all()
    case_id = v1.native_case()
    real = runs_module.load_case

    def load_then_delete(db, cid, user, action):
        case = real(db, cid, user, action)
        # the case is deleted by another request after this request found it
        from app.models._common import utcnow

        with v1.Session() as other:
            other.get(Case, cid).deleted_at = utcnow()
            other.commit()
        return case

    monkeypatch.setattr(runs_module, "load_case", load_then_delete)
    r = v1.start(case_id)
    assert r.status_code == 404 and r.json()["code"] == "case_not_found"
    with v1.Session() as db:
        assert db.scalar(select(PipelineRun)) is None


def test_delete_re_checks_runs_under_the_lock(v1, monkeypatch):
    v1.enable_all()
    case_id = v1.native_case()
    real = cases_module.load_case

    def load_then_start_run(db, cid, user, action):
        case = real(db, cid, user, action)
        if action == "case.delete":
            monkeypatch.setattr(cases_module, "load_case", real)
            assert v1.start(case_id).status_code == 201  # a run is created meanwhile
        return case

    monkeypatch.setattr(cases_module, "load_case", load_then_start_run)
    r = v1.client.delete(f"/api/v1/cases/{case_id}", headers=v1.alice)
    assert r.status_code == 409 and r.json()["code"] == "case_has_active_run"
    with v1.Session() as db:
        assert db.get(Case, uuid.UUID(case_id)).deleted_at is None


# ------------------------------------------------------------------- temp files


def test_uploads_use_private_temp_dirs_and_ignore_leftovers(v1):
    case_id = v1.new_case()
    incoming = v1.root / ".incoming"
    (incoming / "up-crashed").mkdir(parents=True)
    (incoming / "up-crashed" / "upload").write_bytes(b"left by a crashed request")
    other = incoming / "up-concurrent"
    other.mkdir()
    (other / "upload").write_bytes(b"in flight in another request")

    assert v1.upload(case_id, "ct").status_code == 201
    assert (other / "upload").read_bytes() == b"in flight in another request"
    assert (incoming / "up-crashed" / "upload").exists()
    assert sorted(p.name for p in incoming.iterdir()) == ["up-concurrent", "up-crashed"]
    # A failed upload removes only its own directory.
    assert v1.upload(case_id, "ct", data=b"not gzip").status_code == 422
    assert sorted(p.name for p in incoming.iterdir()) == ["up-concurrent", "up-crashed"]


# ------------------------------------------------------------------------ quota


def test_quota_is_checked_under_the_owner_lock(v1, monkeypatch):
    """The owner row is locked before the check, so a concurrent upload sees our bytes."""
    from app.models import User

    case_id = v1.new_case()
    size = len(nifti_gz(b"ct"))
    with v1.Session() as db:
        db.get(User, v1.alice_id).storage_quota_bytes = size + 5
        db.commit()
    order = []
    real_lock = cases_module.lock_user
    real_check = cases_module.check_quota

    def lock(db, uid):
        order.append("lock")
        return real_lock(db, uid)

    def check(*args, **kwargs):
        order.append("check")
        return real_check(*args, **kwargs)

    monkeypatch.setattr(cases_module, "lock_user", lock)
    monkeypatch.setattr(cases_module, "check_quota", check)
    assert v1.upload(case_id, "ct", data=nifti_gz(b"ct")).status_code == 201
    assert order == ["lock", "check"]
    assert v1.upload(case_id, "lesion_native").status_code == 413


# -------------------------------------------------------------- file path guards


def test_downloads_refuse_paths_outside_the_case(v1):
    c = v1.client
    case_a = v1.native_case()
    case_b = v1.native_case()
    with v1.Session() as db:
        other = db.scalar(
            select(InputFile).where(
                InputFile.case_id == uuid.UUID(case_b), InputFile.role == "ct"
            )
        )
        mine = db.scalar(
            select(InputFile).where(
                InputFile.case_id == uuid.UUID(case_a), InputFile.role == "ct"
            )
        )
        assert (
            c.get(
                f"/api/v1/cases/{case_a}/inputs/ct/download", headers=v1.alice
            ).status_code
            == 200
        )
        mine.storage_path = other.storage_path  # points into another case's directory
        db.commit()
    r = c.get(f"/api/v1/cases/{case_a}/inputs/ct/download", headers=v1.alice)
    assert r.status_code == 404 and r.json()["code"] == "file_missing_in_storage"


def test_downloads_refuse_symlinks(v1, tmp_path):
    c = v1.client
    case_id = v1.native_case()
    with v1.Session() as db:
        stored = db.scalar(
            select(InputFile).where(
                InputFile.case_id == uuid.UUID(case_id), InputFile.role == "ct"
            )
        ).storage_path
    path = v1.root / stored
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"secret")
    path.unlink()
    try:
        path.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symbolic links are not available here")
    r = c.get(f"/api/v1/cases/{case_id}/inputs/ct/download", headers=v1.alice)
    assert r.status_code == 404 and r.json()["code"] == "file_missing_in_storage"


def test_artifact_log_and_provenance_paths_are_confined(v1):
    c = v1.client
    case_a, run_a = v1.finished_run()
    case_b, run_b = v1.finished_run()
    with v1.Session() as db:
        art_b = db.scalars(
            select(Artifact).where(Artifact.pipeline_run_id == uuid.UUID(run_b))
        ).first()
        art_a = db.scalars(
            select(Artifact).where(Artifact.pipeline_run_id == uuid.UUID(run_a))
        ).first()
        art_a_id = str(art_a.id)
        art_a.storage_path = art_b.storage_path  # outside the case directory of run A
        run = db.get(PipelineRun, uuid.UUID(run_a))
        run.provenance_path = art_b.storage_path
        db.commit()
    r = c.get(f"/api/v1/runs/{run_a}/artifacts/{art_a_id}/download", headers=v1.alice)
    assert r.status_code == 404 and r.json()["code"] == "file_missing_in_storage"
    r = c.get(f"/api/v1/runs/{run_a}/provenance", headers=v1.alice)
    assert r.status_code == 404 and r.json()["code"] == "provenance_not_available"


# ------------------------------------------------------- parameter refusals: 422


@pytest.mark.parametrize(
    ("code", "status"),
    [
        ("invalid_parameters", 422),
        ("implementation_invalid", 422),
        ("manifest_unavailable", 409),
        ("bundle_unavailable", 409),
    ],
)
def test_resolution_refusals_map_to_status(v1, monkeypatch, code, status):
    v1.enable_all()
    case_id = v1.native_case()

    def refuse(*args, **kwargs):
        raise RunRefused(code, "refused for the test", slot="normalisation")

    monkeypatch.setattr(runs_module, "create_run", refuse)
    r = v1.start(case_id)
    assert r.status_code == status
    assert r.json() == {
        "detail": "refused for the test",
        "code": code,
        "slot": "normalisation",
    }
    with v1.Session() as db:
        assert db.scalar(select(PipelineRun)) is None
        assert "run_created" not in db.scalars(select(AuditEvent.event_type)).all()
