"""Real concurrency of the v1 locks on PostgreSQL (card QA-01).

SQLite ignores ``SELECT ... FOR UPDATE``, so ``test_api_v1_hardening.py`` can only simulate
races sequentially. Here two requests really run at the same time against PostgreSQL: a
gate pauses the first request right after it took its row lock, the second request is
started and must block on that lock (checked in ``pg_stat_activity``), then the gate is
opened and both outcomes are checked. Runs only with ``TEST_DB=postgres`` (CI job
``tests-postgres``); the lock-order scenarios hold whatever the interleaving, so there are
no sleeps that the outcome depends on.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

import app.api.v1.cases as cases_module
import app.api.v1.runs as runs_module
from app.api.v1.deps import lock_case, lock_user
from app.main import app
from app.models import AuditEvent, Case, InputFile, PipelineRun, User
from tests import pgdb
from tests.test_api_v1 import PIPE, V1Env, nifti_gz

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not pgdb.enabled(), reason="needs TEST_DB=postgres"),
]

WAIT = 30  # seconds; only reached when a test is already failing


@pytest.fixture()
def v1(client):
    yield V1Env(client)


class Gate:
    """Pause the first call of a function right after it returned (so after the row lock
    was taken) until ``release`` is set."""

    def __init__(self, real):
        self.real = real
        self.reached = threading.Event()
        self.release = threading.Event()
        self._used = False
        self._mutex = threading.Lock()

    def __call__(self, *args, **kwargs):
        result = self.real(*args, **kwargs)
        with self._mutex:
            first = not self._used
            self._used = True
        if first:
            self.reached.set()
            self.release.wait(WAIT)
        return result


class Request(threading.Thread):
    """One HTTP request in its own thread and its own client (own portal and loop)."""

    def __init__(self, method: str, url: str, **kwargs):
        super().__init__(daemon=True)
        self.method, self.url, self.kwargs = method, url, kwargs
        self.response = None
        self.error: BaseException | None = None

    def run(self):
        try:
            self.response = TestClient(app).request(
                self.method, self.url, **self.kwargs
            )
        except BaseException as exc:  # reported by finish()
            self.error = exc

    def finish(self):
        self.join(WAIT)
        assert not self.is_alive(), "request did not finish"
        assert self.error is None, self.error
        return self.response


def lock_waiters(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        ).scalar_one()


def wait_until_blocked(engine, expected: int = 1) -> None:
    deadline = time.monotonic() + WAIT
    while time.monotonic() < deadline:
        if lock_waiters(engine) >= expected:
            return
        time.sleep(0.05)
    raise AssertionError("the second request never blocked on a row lock")


def engine_of(v1):
    return v1.Session.kw["bind"]


def upload_request(v1, case_id, role, data, headers=None):
    return Request(
        "PUT",
        f"/api/v1/cases/{case_id}/inputs/{role}",
        files={"file": ("x.nii.gz", data, "application/gzip")},
        headers=headers or v1.alice,
    )


def run_request(v1, case_id, headers=None):
    return Request(
        "POST",
        f"/api/v1/cases/{case_id}/runs",
        json={"pipeline_id": PIPE},
        headers=headers or v1.alice,
    )


def delete_request(v1, case_id, headers=None):
    return Request("DELETE", f"/api/v1/cases/{case_id}", headers=headers or v1.alice)


def sha_of(v1, case_id, role="ct"):
    with v1.Session() as db:
        return db.scalar(
            select(InputFile.sha256).where(
                InputFile.case_id == uuid.UUID(case_id), InputFile.role == role
            )
        )


# ---------------------------------------------------------------- the lock itself


def test_lock_case_and_lock_user_really_block_other_sessions(v1):
    case_id = uuid.UUID(v1.native_case())
    with v1.Session() as holder:
        case = lock_case(holder, case_id)
        lock_user(holder, case.owner_user_id)
        with v1.Session() as other:
            other.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(OperationalError, match="lock timeout|LockNotAvailable"):
                lock_case(other, case_id)
            other.rollback()
            other.execute(text("SET LOCAL lock_timeout = '300ms'"))
            with pytest.raises(OperationalError, match="lock timeout|LockNotAvailable"):
                lock_user(other, case.owner_user_id)
            other.rollback()
        holder.rollback()
    with v1.Session() as after:  # released with the transaction
        assert lock_case(after, case_id).id == case_id
        after.rollback()


# ------------------------------------------------- upload (input change) vs run creation


def test_run_creation_waits_for_an_upload_and_sees_its_input(v1, monkeypatch):
    v1.enable_all()
    case_id = v1.native_case()
    gate = Gate(cases_module.lock_case)
    monkeypatch.setattr(cases_module, "lock_case", gate)
    new_bytes = nifti_gz(b"replaced while a run was requested")

    upload = upload_request(v1, case_id, "ct", new_bytes)
    upload.start()
    assert gate.reached.wait(WAIT)
    run = run_request(v1, case_id)
    run.start()
    try:
        wait_until_blocked(engine_of(v1))
        assert run.is_alive()  # waiting for the case row held by the upload
    finally:
        gate.release.set()
    assert upload.finish().status_code == 200
    assert run.finish().status_code == 201
    assert sha_of(v1, case_id) == hashlib.sha256(new_bytes).hexdigest()
    stored = v1.root / case_id
    assert any(
        hashlib.sha256(p.read_bytes()).hexdigest() == sha_of(v1, case_id)
        for p in stored.rglob("*ct*")
        if p.is_file()
    )


def test_upload_waits_for_a_run_creation_and_is_refused(v1, monkeypatch):
    v1.enable_all()
    case_id = v1.native_case()
    before = sha_of(v1, case_id)
    gate = Gate(runs_module.lock_case)
    monkeypatch.setattr(runs_module, "lock_case", gate)

    run = run_request(v1, case_id)
    run.start()
    assert gate.reached.wait(WAIT)
    # The upload passes its early check (no run committed yet) and then blocks.
    upload = upload_request(v1, case_id, "ct", nifti_gz(b"too late"))
    upload.start()
    try:
        wait_until_blocked(engine_of(v1))
        assert upload.is_alive()
    finally:
        gate.release.set()
    assert run.finish().status_code == 201
    refused = upload.finish()
    assert refused.status_code == 409 and refused.json()["code"] == "case_has_runs"
    assert sha_of(v1, case_id) == before


# ----------------------------------------------------------- delete vs run creation


def test_run_creation_waits_for_a_delete_and_finds_the_case_gone(v1, monkeypatch):
    v1.enable_all()
    case_id = v1.native_case()
    gate = Gate(cases_module.lock_case)
    monkeypatch.setattr(cases_module, "lock_case", gate)

    delete = delete_request(v1, case_id)
    delete.start()
    assert gate.reached.wait(WAIT)
    run = run_request(v1, case_id)
    run.start()
    try:
        wait_until_blocked(engine_of(v1))
        assert run.is_alive()
    finally:
        gate.release.set()
    assert delete.finish().status_code == 204
    gone = run.finish()
    assert gone.status_code == 404
    with v1.Session() as db:
        assert db.scalars(select(PipelineRun)).first() is None
        assert db.get(Case, uuid.UUID(case_id)).deleted_at is not None


def test_delete_waits_for_a_run_creation_and_is_refused(v1, monkeypatch):
    v1.enable_all()
    case_id = v1.native_case()
    gate = Gate(runs_module.lock_case)
    monkeypatch.setattr(runs_module, "lock_case", gate)

    run = run_request(v1, case_id)
    run.start()
    assert gate.reached.wait(WAIT)
    delete = delete_request(v1, case_id)
    delete.start()
    try:
        wait_until_blocked(engine_of(v1))
        assert delete.is_alive()
    finally:
        gate.release.set()
    assert run.finish().status_code == 201
    refused = delete.finish()
    assert (
        refused.status_code == 409 and refused.json()["code"] == "case_has_active_run"
    )
    with v1.Session() as db:
        assert db.get(Case, uuid.UUID(case_id)).deleted_at is None


# ------------------------------------------------------------- quota, two uploads


def test_parallel_uploads_of_one_owner_cannot_both_fit_the_quota(v1, monkeypatch):
    first_case = v1.new_case()
    second_case = v1.new_case()
    data = nifti_gz(b"q" * 64)
    with v1.Session() as db:
        # Room for one of the two files, not for both.
        db.get(User, v1.alice_id).storage_quota_bytes = len(data) + 10
        db.commit()
    gate = Gate(cases_module.lock_user)
    monkeypatch.setattr(cases_module, "lock_user", gate)

    first = upload_request(v1, first_case, "ct", data)
    first.start()
    assert gate.reached.wait(WAIT)  # the first upload holds case 1 and the owner row
    second = upload_request(v1, second_case, "ct", data)  # other case, same owner
    second.start()
    try:
        wait_until_blocked(engine_of(v1))
        assert second.is_alive()
    finally:
        gate.release.set()
    statuses = sorted([first.finish().status_code, second.finish().status_code])
    assert statuses == [201, 413]
    with v1.Session() as db:
        stored = db.scalars(select(InputFile)).all()
        assert len(stored) == 1


def test_unsynchronised_parallel_uploads_respect_the_quota(v1):
    """No gate: whichever order the two requests take, at most one fits."""
    cases = [v1.new_case() for _ in range(4)]
    data = nifti_gz(b"r" * 64)
    with v1.Session() as db:
        db.get(User, v1.alice_id).storage_quota_bytes = 2 * len(data) + 10
        db.commit()
    requests = [upload_request(v1, c, "ct", data) for c in cases]
    for r in requests:
        r.start()
    statuses = sorted(r.finish().status_code for r in requests)
    assert statuses == [201, 201, 413, 413]


# ------------------------------------------------------------- audit rows and FKs


def test_every_v1_write_leaves_an_audit_row_without_fk_errors(v1):
    """Each audit_events insert references rows created in the same request; PostgreSQL
    enforces the order that SQLite does not."""
    v1.enable_all()
    case_id = v1.new_case()
    c = v1.client
    assert (
        c.patch(
            f"/api/v1/cases/{case_id}", json={"sex": "female"}, headers=v1.alice
        ).status_code
        == 200
    )
    assert v1.upload(case_id, "ct").status_code == 201
    assert v1.upload(case_id, "lesion_native").status_code == 201
    assert (
        c.delete(
            f"/api/v1/cases/{case_id}/inputs/lesion_native", headers=v1.alice
        ).status_code
        == 204
    )
    assert v1.upload(case_id, "lesion_native").status_code == 201
    started = v1.start(case_id)
    assert started.status_code == 201, started.text
    run_id = started.json()["id"]
    assert c.post(f"/api/v1/runs/{run_id}/cancel", headers=v1.alice).status_code == 200
    assert c.delete(f"/api/v1/cases/{case_id}", headers=v1.alice).status_code == 204

    with v1.Session() as db:
        events = [
            (e.event_type, e.case_id, e.pipeline_run_id)
            for e in db.scalars(select(AuditEvent).order_by(AuditEvent.timestamp))
        ]
    types = {t for t, _c, _r in events}
    assert {
        "implementation_setting_changed",
        "case_created",
        "case_updated",
        "input_uploaded",
        "input_deleted",
        "run_created",
        "run_canceled",
        "case_deleted",
    } <= types
    for event_type, event_case, event_run in events:
        if event_type.startswith(("case_", "input_", "run_")):
            assert str(event_case) == case_id, event_type
        if event_type.startswith("run_"):
            assert str(event_run) == run_id, event_type
    assert json.loads(db_details(v1, "run_created"))  # details are stored as JSON text


def db_details(v1, event_type):
    with v1.Session() as db:
        return db.scalar(
            select(AuditEvent.details).where(AuditEvent.event_type == event_type)
        )
