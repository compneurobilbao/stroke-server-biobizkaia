"""Case and run API (`/api/v1`, card F0-30): every flow and every permission boundary.

The pipeline, step manifests and the in-process executor are those of the orchestrator
tests, so runs are driven to a real final state through ``Orchestrator.tick``. All
files are synthetic bytes.
"""

from __future__ import annotations

import gzip
import json
import struct
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.api.v1.deps import Orchestration, get_orchestration, get_run_queue
from app.api.v1.openapi import build_openapi
from app.core.config import get_settings
from app.main import app
from app.models import (
    Artifact,
    AuditEvent,
    Case,
    InputFile,
    PipelineRun,
    Report,
    StepRun,
    User,
)
from orchestrator.catalog import StaticPipelineRegistry, StaticStepCatalog
from orchestrator.engine import (
    ExecutorRegistry,
    Orchestrator,
    OrchestratorConfig,
    TickResult,
)
from orchestrator.pipeline import load_pipeline
from orchestrator.store import RunStore
from tests.test_api import auth_headers, create_test_user
from tests.test_orchestrator import (
    MANIFESTS,
    PIPELINE_YAML,
    FakeExecutor,
    _Settings,
)

OPENAPI_SNAPSHOT = (
    Path(__file__).resolve().parents[1] / "docs" / "api" / "openapi-v1.json"
)
PIPE = "test-flow"


def nifti_gz(extra: bytes = b"") -> bytes:
    """Smallest byte string that looks like a gzipped NIfTI-1 file (header only)."""
    header = bytearray(348)
    struct.pack_into("<i", header, 0, 348)
    header[344:348] = b"n+1\x00"
    return gzip.compress(bytes(header) + b"\x00\x00\x00\x00" + extra)


class FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[uuid.UUID] = []
        self.revoked: list[str] = []

    def enqueue(self, run_id):
        self.enqueued.append(run_id)
        return f"task-{len(self.enqueued)}"

    def revoke(self, task_id):
        self.revoked.append(task_id)


class NoBundles:
    def resolve(self, requirements, selection=None):
        return {}


class V1Env:
    def __init__(self, client, script=None):
        self.client = client
        self.Session = client.app.state.testing_session_local
        self.root = Path(get_settings().storage_root)
        pipeline = load_pipeline(PIPELINE_YAML.encode())
        self.services = Orchestration(
            pipelines=StaticPipelineRegistry([pipeline]),
            steps=StaticStepCatalog(MANIFESTS.values()),
            bundles=NoBundles(),
            platform_version="0.0.0-test",
        )
        self.queue = FakeQueue()
        self.executor = FakeExecutor(script)
        self.orchestrator = Orchestrator(
            self.Session,
            pipelines=self.services.pipelines,
            steps=self.services.steps,
            executors=ExecutorRegistry(
                {self.executor.name: self.executor}, self.executor.name
            ),
            bundles=self.services.bundles,
            store=RunStore(self.root),
            settings=_Settings(),
            config=OrchestratorConfig(poll_seconds=0, max_attempts=1),
        )
        app.dependency_overrides[get_orchestration] = lambda: self.services
        app.dependency_overrides[get_run_queue] = lambda: self.queue
        self.admin, self.admin_id = auth_headers(
            client, "admin@example.org", "secret-pass", role="admin"
        )
        self.alice, self.alice_id = auth_headers(
            client, "alice@example.org", "secret-pass"
        )
        self.bob, self.bob_id = auth_headers(client, "bob@example.org", "secret-pass")

    # --- helpers
    def enable_all(self):
        for slot, impl in (
            ("intake", "intake"),
            ("normalisation", "norm"),
            ("disconnection", "disc"),
        ):
            r = self.client.put(
                f"/api/v1/admin/implementations/{PIPE}/{slot}/{impl}",
                json={"enabled": True, "is_default": True},
                headers=self.admin,
            )
            assert r.status_code == 200, r.text

    def new_case(self, headers=None, **body):
        r = self.client.post("/api/v1/cases", json=body, headers=headers or self.alice)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def upload(self, case_id, role, headers=None, data=None, name="x.nii.gz"):
        return self.client.put(
            f"/api/v1/cases/{case_id}/inputs/{role}",
            files={"file": (name, data or nifti_gz(role.encode()), "application/gzip")},
            headers=headers or self.alice,
        )

    def native_case(self, headers=None):
        case_id = self.new_case(headers)
        for role in ("ct", "lesion_native"):
            assert self.upload(case_id, role, headers).status_code == 201
        return case_id

    def start(self, case_id, headers=None, **body):
        return self.client.post(
            f"/api/v1/cases/{case_id}/runs",
            json={"pipeline_id": PIPE, **body},
            headers=headers or self.alice,
        )

    def drive(self, run_id, max_ticks=60):
        # The Celery task id is the lease owner, as in worker.tasks.run_pipeline.
        with self.Session() as db:
            owner = db.get(PipelineRun, uuid.UUID(run_id)).orchestrator_task_id
        for _ in range(max_ticks):
            result = self.orchestrator.tick(uuid.UUID(run_id), owner or "task-x")
            if result is not TickResult.CONTINUE:
                return result
        raise AssertionError("run did not finish")

    def finished_run(self, headers=None):
        self.enable_all()
        case_id = self.native_case(headers)
        run = self.start(case_id, headers).json()
        assert self.drive(run["id"]) is TickResult.FINISHED
        return case_id, run["id"]


@pytest.fixture()
def v1(client):
    yield V1Env(client)


# ----------------------------------------------------------------- pipelines


def test_pipeline_slots_list_only_enabled_implementations(v1):
    c = v1.client
    listing = c.get("/api/v1/pipelines", headers=v1.alice)
    assert listing.status_code == 200
    assert listing.json()[0]["id"] == PIPE
    assert [s["name"] for s in listing.json()[0]["input_sets"]] == ["native", "mni"]

    base = f"/api/v1/pipelines/{PIPE}/versions/1.0.0"
    slots = c.get(f"{base}/slots", headers=v1.alice).json()
    assert [s["name"] for s in slots] == ["intake", "normalisation", "disconnection"]
    assert all(
        s["implementations"] == [] and s["default_implementation"] is None
        for s in slots
    )

    v1.enable_all()
    slots = {s["name"]: s for s in c.get(f"{base}/slots", headers=v1.alice).json()}
    norm = slots["normalisation"]
    assert norm["default_implementation"] == "norm"
    assert norm["implementations"][0]["id"] == "norm"
    assert (
        norm["implementations"][0]["enabled"]
        and norm["implementations"][0]["available"]
    )
    assert norm["consumes"][0] == {"port": "ct", "type": "ct_native", "optional": False}
    assert c.get(base, headers=v1.alice).json()["slots"][0]["name"] == "intake"

    assert (
        c.get(f"/api/v1/pipelines/{PIPE}/versions/9.9.9", headers=v1.alice).status_code
        == 404
    )
    assert (
        c.get("/api/v1/pipelines/none/versions/1.0.0/slots", headers=v1.alice).json()[
            "code"
        ]
        == "pipeline_not_found"
    )


def test_disabled_implementations_are_visible_to_admins_only(v1):
    base = f"/api/v1/pipelines/{PIPE}/versions/1.0.0/slots"
    assert (
        v1.client.get(
            base, params={"include_disabled": True}, headers=v1.alice
        ).status_code
        == 403
    )
    admin_view = v1.client.get(
        base, params={"include_disabled": True}, headers=v1.admin
    ).json()
    assert admin_view[0]["implementations"][0]["enabled"] is False


def test_pipelines_need_login_and_a_permitted_role(v1):
    assert v1.client.get("/api/v1/pipelines").status_code == 401
    create_test_user(v1.client, "svc@example.org", "secret-pass", role="service")
    token = v1.client.post(
        "/api/auth/login", json={"email": "svc@example.org", "password": "secret-pass"}
    ).json()["access_token"]
    service = {"Authorization": f"Bearer {token}"}
    assert v1.client.get("/api/v1/pipelines", headers=service).status_code == 403
    assert v1.client.get("/api/v1/cases", headers=service).status_code == 403
    reviewer, _ = auth_headers(
        v1.client, "rev@example.org", "secret-pass", role="reviewer"
    )
    assert v1.client.get("/api/v1/pipelines", headers=reviewer).status_code == 200


# --------------------------------------------------------------------- admin


def test_admin_enables_disables_and_sets_defaults(v1):
    c = v1.client
    url = f"/api/v1/admin/implementations/{PIPE}/intake/intake"
    assert c.put(url, json={"enabled": True}, headers=v1.alice).status_code == 403
    assert c.get("/api/v1/admin/implementations", headers=v1.alice).status_code == 403
    assert c.put(url, json={"enabled": True}).status_code == 401

    listing = c.get("/api/v1/admin/implementations", headers=v1.admin).json()
    assert len(listing) == 3 and not any(i["enabled"] for i in listing)

    ok = c.put(url, json={"enabled": True, "is_default": True}, headers=v1.admin)
    assert ok.status_code == 200
    assert ok.json()["enabled"] and ok.json()["is_default"]
    assert ok.json()["updated_by_user_id"] == str(v1.admin_id)
    # A default must be enabled; unknown implementations are refused.
    assert (
        c.put(
            url, json={"enabled": False, "is_default": True}, headers=v1.admin
        ).status_code
        == 422
    )
    missing = c.put(
        f"/api/v1/admin/implementations/{PIPE}/intake/nope",
        json={"enabled": True},
        headers=v1.admin,
    )
    assert (
        missing.status_code == 404
        and missing.json()["code"] == "implementation_not_found"
    )

    off = c.put(url, json={"enabled": False}, headers=v1.admin).json()
    assert off["enabled"] is False and off["is_default"] is False
    filtered = c.get(
        "/api/v1/admin/implementations",
        params={"pipeline_id": "other"},
        headers=v1.admin,
    )
    assert filtered.json() == []
    with v1.Session() as db:
        events = db.scalars(
            select(AuditEvent.event_type).where(
                AuditEvent.event_type == "implementation_setting_changed"
            )
        ).all()
        assert len(events) == 2


# --------------------------------------------------------------------- cases


def test_case_lifecycle_and_metadata_privacy(v1):
    c = v1.client
    case_id = v1.new_case(sex="female", age_band="65-69")
    detail = c.get(f"/api/v1/cases/{case_id}", headers=v1.alice).json()
    assert detail["owner_user_id"] == str(v1.alice_id)
    assert detail["age_band"] == "65-69" and detail["inputs"] == []
    assert (
        len(detail["subject_label"]) == 12
        and detail["deployment_profile"] == "research-public"
    )

    # Only a band is accepted: no exact age, no free-text identifiers.
    for bad in (
        {"age": 67},
        {"age_band": "67"},
        {"age_band": "65-70"},
        {"subject_label": "abc"},
        {"name": "x"},
    ):
        assert c.post("/api/v1/cases", json=bad, headers=v1.alice).status_code == 422

    patched = c.patch(
        f"/api/v1/cases/{case_id}",
        json={"age_band": None, "sex": "male"},
        headers=v1.alice,
    )
    assert patched.status_code == 200
    assert patched.json()["age_band"] is None and patched.json()["sex"] == "male"
    assert (
        c.patch(
            f"/api/v1/cases/{case_id}", json={"age_band": "7"}, headers=v1.alice
        ).status_code
        == 422
    )

    listed = c.get("/api/v1/cases", headers=v1.alice).json()
    assert [x["id"] for x in listed] == [case_id]
    assert (
        c.get("/api/v1/cases", params={"limit": 0}, headers=v1.alice).status_code == 422
    )

    assert c.delete(f"/api/v1/cases/{case_id}", headers=v1.alice).status_code == 204
    assert c.get(f"/api/v1/cases/{case_id}", headers=v1.alice).status_code == 404
    assert c.get("/api/v1/cases", headers=v1.alice).json() == []
    with v1.Session() as db:
        assert db.get(Case, uuid.UUID(case_id)).deleted_at is not None


def test_inputs_upload_replace_download_and_validation(v1):
    c = v1.client
    case_id = v1.new_case()
    first = v1.upload(case_id, "ct", name="Patient Name 1234.nii.gz")
    assert first.status_code == 201
    body = first.json()
    assert body["role"] == "ct" and body["artefact_type"] == "ct_native"
    assert len(body["sha256"]) == 64 and body["size_bytes"] > 0

    again = v1.upload(case_id, "ct", data=nifti_gz(b"other"))
    assert again.status_code == 200 and again.json()["id"] == body["id"]
    assert again.json()["sha256"] != body["sha256"]

    download = c.get(f"/api/v1/cases/{case_id}/inputs/ct/download", headers=v1.alice)
    assert download.status_code == 200
    assert gzip.decompress(download.content)[:4] == struct.pack("<i", 348)
    assert "Patient" not in download.headers["content-disposition"]
    assert [
        i["role"]
        for i in c.get(f"/api/v1/cases/{case_id}/inputs", headers=v1.alice).json()
    ] == ["ct"]

    # The uploaded file name is stored nowhere: not in the database, not in the audit log.
    with v1.Session() as db:
        stored = db.scalar(select(InputFile))
        assert "Patient" not in stored.storage_path
        for event in db.scalars(select(AuditEvent)):
            assert "Patient" not in (event.details or "")
    assert not any("Patient" in str(p) for p in v1.root.rglob("*"))

    assert v1.upload(case_id, "ct", name="scan.nii").status_code == 422
    assert v1.upload(case_id, "unknown_role").json()["code"] == "unknown_input_role"
    assert (
        v1.upload(case_id, "ct", data=b"not gzip").json()["code"]
        == "invalid_input_file"
    )
    assert (
        v1.upload(case_id, "ct", data=gzip.compress(b"x" * 400)).json()["code"]
        == "invalid_input_file"
    )
    assert v1.upload(case_id, "ct", data=gzip.compress(b"")).status_code == 422
    assert (
        c.get(
            f"/api/v1/cases/{case_id}/inputs/lesion_mni/download", headers=v1.alice
        ).json()["code"]
        == "input_not_found"
    )

    assert (
        c.delete(f"/api/v1/cases/{case_id}/inputs/ct", headers=v1.alice).status_code
        == 204
    )
    assert c.get(f"/api/v1/cases/{case_id}/inputs", headers=v1.alice).json() == []


def test_upload_size_limit(v1, monkeypatch):
    monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", "0")
    get_settings.cache_clear()
    case_id = v1.new_case()
    r = v1.upload(case_id, "ct")
    assert r.status_code == 413 and r.json()["code"] == "upload_too_large"
    assert not list((v1.root / case_id).glob("incoming/*"))


def test_quota_counts_inputs_and_artefacts(v1):
    v1.enable_all()
    case_id = v1.new_case()
    size = len(nifti_gz(b"ct"))
    with v1.Session() as db:
        db.get(User, v1.alice_id).storage_quota_bytes = size + 10
        db.commit()
    assert v1.upload(case_id, "ct", data=nifti_gz(b"ct")).status_code == 201
    over = v1.upload(case_id, "lesion_native", data=nifti_gz(b"mask"))
    assert over.status_code == 413 and over.json()["code"] == "storage_quota_exceeded"
    # Replacing a file counts only the difference.
    assert v1.upload(case_id, "ct", data=nifti_gz(b"ct")).status_code == 200

    # A user who is at or over the quota cannot start runs, even through an admin.
    with v1.Session() as db:
        db.get(User, v1.alice_id).storage_quota_bytes = size
        db.commit()
    assert v1.upload(case_id, "lesion_native").status_code == 413
    with v1.Session() as db:
        db.get(User, v1.alice_id).storage_quota_bytes = 1
        db.commit()
    blocked = v1.start(case_id, headers=v1.admin)
    assert (
        blocked.status_code == 413
        and blocked.json()["code"] == "storage_quota_exceeded"
    )

    # Artefacts of finished runs count too.
    with v1.Session() as db:
        db.get(User, v1.alice_id).storage_quota_bytes = None
        db.commit()
    case2, _run = v1.finished_run()
    from app.api.v1.quota import storage_used

    with v1.Session() as db:
        inputs = sum(i.size_bytes for i in db.scalars(select(InputFile)))
        artefacts = sum(a.size_bytes for a in db.scalars(select(Artifact)))
        assert artefacts > 0
        used = storage_used(db, v1.alice_id)
        assert used == inputs + artefacts


def test_legacy_cases_are_not_served(v1):
    with v1.Session() as db:
        case = Case(owner_user_id=v1.alice_id, deployment_profile="legacy")
        db.add(case)
        db.commit()
        legacy_id = str(case.id)
    assert (
        v1.client.get(f"/api/v1/cases/{legacy_id}", headers=v1.alice).status_code == 404
    )
    assert (
        v1.client.get(f"/api/v1/cases/{legacy_id}", headers=v1.admin).status_code == 404
    )
    assert v1.client.get("/api/v1/cases", headers=v1.admin).json() == []


# ---------------------------------------------------------------------- runs


def test_run_flow_native_mode_end_to_end(v1):
    c = v1.client
    v1.enable_all()
    case_id = v1.native_case()
    created = v1.start(case_id, implementations={"normalisation": "norm"}, seed=7)
    assert created.status_code == 201, created.text
    run = created.json()
    assert (
        run["status"] == "queued" and run["input_set"] == "native" and run["seed"] == 7
    )
    assert run["accounting_submitted_by"] == f"user:{v1.alice_id}"
    assert run["pipeline_version"] == "1.0.0" and len(run["pipeline_sha256"]) == 64
    by_slot = {s["slot"]: s for s in run["slots"]}
    assert by_slot["normalisation"]["status"] == "selected"
    assert by_slot["normalisation"]["chosen_by"] == "user"
    assert by_slot["intake"]["chosen_by"] == "deployment_default"
    assert sorted(i["role"] for i in run["inputs"]) == ["ct", "lesion_native"]
    assert v1.queue.enqueued == [uuid.UUID(run["id"])]
    with v1.Session() as db:
        assert (
            db.get(PipelineRun, uuid.UUID(run["id"])).orchestrator_task_id == "task-1"
        )
        assert "run_created" in db.scalars(select(AuditEvent.event_type)).all()

    assert v1.drive(run["id"]) is TickResult.FINISHED
    done = c.get(f"/api/v1/runs/{run['id']}", headers=v1.alice).json()
    assert done["status"] == "succeeded" and done["finished_at"]
    timeline = [
        (s["slot"], s["attempt"], s["status"], s["outcome"]) for s in done["steps"]
    ]
    assert ("normalisation", 1, "succeeded", "succeeded") in timeline
    assert c.get(f"/api/v1/runs/{run['id']}/steps", headers=v1.alice).json()[0][
        "slot"
    ] in {"intake", "normalisation"}
    assert [
        r["id"] for r in c.get(f"/api/v1/cases/{case_id}/runs", headers=v1.alice).json()
    ] == [run["id"]]

    artifacts = c.get(f"/api/v1/runs/{run['id']}/artifacts", headers=v1.alice).json()
    assert {a["port"] for a in artifacts} >= {"tract_disconnection", "lesion_mask_mni"}
    target = next(a for a in artifacts if a["port"] == "tract_disconnection")
    assert target["filename"].endswith(".tsv") and target["synthetic"] in (True, False)
    dl = c.get(
        f"/api/v1/runs/{run['id']}/artifacts/{target['id']}/download", headers=v1.alice
    )
    assert dl.status_code == 200 and len(dl.content) == target["size_bytes"]

    prov = c.get(f"/api/v1/runs/{run['id']}/provenance", headers=v1.alice).json()
    assert prov["run_id"] == run["id"] and prov["pipeline"]["id"] == PIPE
    assert c.get(f"/api/v1/runs/{run['id']}/reports", headers=v1.alice).json() == []
    assert (
        c.get(
            f"/api/v1/runs/{run['id']}/reports/{uuid.uuid4()}/pdf", headers=v1.alice
        ).json()["code"]
        == "report_not_found"
    )
    assert (
        c.get(
            f"/api/v1/runs/{run['id']}/artifacts/{uuid.uuid4()}/download",
            headers=v1.alice,
        ).json()["code"]
        == "artifact_not_found"
    )

    # Inputs are frozen once a run exists.
    assert v1.upload(case_id, "ct").json()["code"] == "case_has_runs"
    assert (
        c.delete(f"/api/v1/cases/{case_id}/inputs/ct", headers=v1.alice).status_code
        == 409
    )


def test_run_flow_mni_mask_only_mode(v1):
    v1.enable_all()
    case_id = v1.new_case()
    assert v1.upload(case_id, "lesion_mni").status_code == 201
    run = v1.start(case_id).json()
    assert run["input_set"] == "mni"
    slots = {s["slot"]: s for s in run["slots"]}
    assert slots["normalisation"]["status"] == "skipped"
    assert slots["normalisation"]["reason"] == "condition_false"
    assert v1.drive(run["id"]) is TickResult.FINISHED
    assert (
        v1.client.get(f"/api/v1/runs/{run['id']}", headers=v1.alice).json()["status"]
        == "succeeded"
    )


def test_input_roles_subset_selects_the_input_set(v1):
    v1.enable_all()
    case_id = v1.native_case()
    assert v1.upload(case_id, "lesion_mni").status_code == 201
    both = v1.start(case_id)  # all three roles match no input set
    assert both.status_code == 422 and both.json()["code"] == "no_matching_input_set"
    ok = v1.start(case_id, input_roles=["lesion_mni"])
    assert ok.status_code == 201 and ok.json()["input_set"] == "mni"
    missing = v1.start(case_id, headers=v1.admin, input_roles=["nope"])
    assert missing.status_code == 422 and missing.json()["code"] == "input_not_found"


def test_run_creation_validates_choices_and_state(v1):
    v1.enable_all()
    case_id = v1.native_case()

    unknown_slot = v1.start(case_id, implementations={"nope": "x"})
    assert (
        unknown_slot.status_code == 422
        and unknown_slot.json()["code"] == "unknown_slot"
    )
    not_allowed = v1.start(case_id, implementations={"normalisation": "elastix"})
    assert not_allowed.status_code == 422
    assert not_allowed.json()["code"] == "implementation_not_enabled"
    assert not_allowed.json()["slot"] == "normalisation"
    assert (
        v1.start(case_id, pipeline_version="9.9.9").json()["code"]
        == "pipeline_not_found"
    )
    assert v1.start(case_id, extra="x").status_code == 422
    assert v1.queue.enqueued == []

    # Disabled by the administrator: a user choice is refused (422), no choice with no
    # other implementation is a 409 naming the slot.
    off = f"/api/v1/admin/implementations/{PIPE}/normalisation/norm"
    v1.client.put(off, json={"enabled": False}, headers=v1.admin)
    refused = v1.start(case_id, implementations={"normalisation": "norm"})
    assert refused.status_code == 422 and refused.json()["slot"] == "normalisation"
    none = v1.start(case_id)
    assert none.status_code == 409 and none.json()["code"] == "no_implementation"
    assert none.json()["slot"] == "normalisation"
    with v1.Session() as db:
        assert db.scalar(select(PipelineRun)) is None  # nothing written when refused

    empty = v1.new_case()
    assert v1.start(empty).json()["code"] == "no_inputs"


def test_only_one_active_run_per_case(v1):
    v1.enable_all()
    case_id = v1.native_case()
    assert v1.start(case_id).status_code == 201
    second = v1.start(case_id)
    assert second.status_code == 409 and second.json()["code"] == "active_run_exists"
    delete = v1.client.delete(f"/api/v1/cases/{case_id}", headers=v1.alice)
    assert delete.status_code == 409 and delete.json()["code"] == "case_has_active_run"


def test_enqueue_failure_leaves_the_run_queued(v1):
    v1.enable_all()

    def boom(run_id):
        raise RuntimeError("broker down")

    v1.queue.enqueue = boom
    r = v1.start(v1.native_case())
    assert r.status_code == 201 and r.json()["status"] == "queued"


def test_step_log_download_and_tail(v1):
    c = v1.client
    _case, run_id = v1.finished_run()
    with v1.Session() as db:
        step = db.scalars(
            select(StepRun).where(StepRun.pipeline_run_id == uuid.UUID(run_id))
        ).first()
        assert step is not None
        rel = f"{_case}/logs-test/step.log"
        path = v1.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"line one\n" + b"x" * 100 + b"\nlast line\n")
        step.log_path = rel
        step_id = str(step.id)
        db.commit()
    log = c.get(f"/api/v1/runs/{run_id}/steps/{step_id}/log", headers=v1.alice)
    assert log.status_code == 200 and "last line" in log.text
    assert log.headers["x-log-truncated"] == "false"
    tail = c.get(
        f"/api/v1/runs/{run_id}/steps/{step_id}/log",
        params={"tail_bytes": 12},
        headers=v1.alice,
    )
    assert (
        tail.text == "x\nlast line\n"[-12:]
        and tail.headers["x-log-truncated"] == "true"
    )
    steps = c.get(f"/api/v1/runs/{run_id}/steps", headers=v1.alice).json()
    assert any(s["log_available"] for s in steps)
    assert (
        c.get(
            f"/api/v1/runs/{run_id}/steps/{uuid.uuid4()}/log", headers=v1.alice
        ).json()["code"]
        == "step_run_not_found"
    )
    with v1.Session() as db:
        other = db.scalars(
            select(StepRun).where(StepRun.id != uuid.UUID(step_id))
        ).first()
        other.log_path = None
        other_id = str(other.id)
        db.commit()
    assert (
        c.get(f"/api/v1/runs/{run_id}/steps/{other_id}/log", headers=v1.alice).json()[
            "code"
        ]
        == "log_not_available"
    )


# ---------------------------------------------------------------- cancel, resume


def test_cancel_queued_run_is_idempotent_and_revokes_the_task(v1):
    c = v1.client
    v1.enable_all()
    run = v1.start(v1.native_case()).json()
    first = c.post(f"/api/v1/runs/{run['id']}/cancel", headers=v1.alice)
    assert first.status_code == 200
    assert first.json() == {
        "run_id": run["id"],
        "status": "canceled",
        "cancel_requested": True,
        "changed": True,
    }
    assert v1.queue.revoked == ["task-1"]
    again = c.post(f"/api/v1/runs/{run['id']}/cancel", headers=v1.alice)
    assert again.status_code == 200 and again.json()["changed"] is False
    assert v1.queue.revoked == ["task-1"]
    with v1.Session() as db:
        events = db.scalars(
            select(AuditEvent.event_type).where(AuditEvent.event_type == "run_canceled")
        ).all()
        assert len(events) == 1


def test_cancel_running_run_returns_202_then_finished_run_is_refused(v1):
    c = v1.client
    v1.enable_all()
    run = v1.start(v1.native_case()).json()
    with v1.Session() as db:
        db.get(PipelineRun, uuid.UUID(run["id"])).status = "running"
        db.commit()
    first = c.post(f"/api/v1/runs/{run['id']}/cancel", headers=v1.alice)
    assert first.status_code == 202 and first.json()["cancel_requested"] is True
    second = c.post(f"/api/v1/runs/{run['id']}/cancel", headers=v1.alice)
    assert second.status_code == 202 and second.json()["changed"] is False
    assert (
        c.get(f"/api/v1/runs/{run['id']}", headers=v1.alice).json()["cancel_requested"]
        is True
    )
    resume = c.post(f"/api/v1/runs/{run['id']}/resume", headers=v1.alice)
    assert resume.status_code == 409 and resume.json()["code"] == "cancel_in_progress"

    _case, done = v1.finished_run(v1.bob)
    refused = c.post(f"/api/v1/runs/{done}/cancel", headers=v1.bob)
    assert refused.status_code == 409 and refused.json()["code"] == "run_not_active"


def test_resume_from_failed_step_keeps_succeeded_slots(client):
    env = V1Env(client, script={"norm-step": ["fail", "ok"]})
    c = client
    env.enable_all()
    case_id = env.native_case()
    run = env.start(case_id).json()
    assert env.drive(run["id"]) is TickResult.FINISHED
    failed = c.get(f"/api/v1/runs/{run['id']}", headers=env.alice).json()
    assert failed["status"] == "failed" and failed["failed_step_run_id"]
    assert failed["failure_code"] == "step_failed"
    env.queue.enqueued.clear()

    resumed = c.post(f"/api/v1/runs/{run['id']}/resume", headers=env.alice)
    assert resumed.status_code == 202 and resumed.json()["status"] == "queued"
    assert env.queue.enqueued == [uuid.UUID(run["id"])]
    # Resuming again is idempotent: the run is already active.
    again = c.post(f"/api/v1/runs/{run['id']}/retry", headers=env.alice)
    assert again.status_code == 200 and again.json()["changed"] is False
    assert env.drive(run["id"]) is TickResult.FINISHED
    final = c.get(f"/api/v1/runs/{run['id']}", headers=env.alice).json()
    assert final["status"] == "succeeded" and final["resume_count"] == 1
    attempts = [(s["slot"], s["trigger"]) for s in final["steps"]]
    assert attempts.count(("intake", "initial")) == 1  # kept, not re-run
    assert ("normalisation", "user_resume") in attempts
    done = c.post(f"/api/v1/runs/{run['id']}/resume", headers=env.alice)
    assert done.status_code == 409 and done.json()["code"] == "run_succeeded"


def test_resume_refusals(client):
    env = V1Env(client, script={"intake-step": ["reject"]})
    c = client
    env.enable_all()
    run = env.start(env.native_case()).json()
    assert env.drive(run["id"]) is TickResult.FINISHED
    rejected = c.post(f"/api/v1/runs/{run['id']}/resume", headers=env.alice)
    assert rejected.status_code == 409 and rejected.json()["code"] == "input_rejected"

    # A canceled run whose implementation was disabled meanwhile cannot be resumed.
    case_id = env.native_case()
    queued = env.start(case_id).json()
    c.post(f"/api/v1/runs/{queued['id']}/cancel", headers=env.alice)
    c.put(
        f"/api/v1/admin/implementations/{PIPE}/intake/intake",
        json={"enabled": False},
        headers=env.admin,
    )
    disabled = c.post(f"/api/v1/runs/{queued['id']}/resume", headers=env.alice)
    assert (
        disabled.status_code == 409
        and disabled.json()["code"] == "implementation_disabled"
    )
    c.put(
        f"/api/v1/admin/implementations/{PIPE}/intake/intake",
        json={"enabled": True},
        headers=env.admin,
    )

    # The deployment no longer loads the pipeline.
    env.services = Orchestration(
        pipelines=StaticPipelineRegistry([]),
        steps=env.services.steps,
        bundles=env.services.bundles,
        platform_version="x",
    )
    app.dependency_overrides[get_orchestration] = lambda: env.services
    gone = c.post(f"/api/v1/runs/{queued['id']}/resume", headers=env.alice)
    assert gone.status_code == 409 and gone.json()["code"] == "pipeline_unavailable"


def test_resume_refused_while_another_run_is_active(client):
    env = V1Env(client, script={"norm-step": ["fail"]})
    env.enable_all()
    case_id = env.native_case()
    run = env.start(case_id).json()
    assert env.drive(run["id"]) is TickResult.FINISHED
    other = env.start(case_id)  # allowed: the failed run is not active
    assert other.status_code == 201
    r = client.post(f"/api/v1/runs/{run['id']}/resume", headers=env.alice)
    assert r.status_code == 409 and r.json()["code"] == "active_run_exists"


# --------------------------------------------------------------- permissions


def test_authentication_is_required_everywhere(v1):
    _case, run_id = v1.finished_run()
    case_id = _case
    calls = [
        ("get", "/api/v1/cases"),
        ("post", "/api/v1/cases"),
        ("get", f"/api/v1/cases/{case_id}"),
        ("patch", f"/api/v1/cases/{case_id}"),
        ("delete", f"/api/v1/cases/{case_id}"),
        ("put", f"/api/v1/cases/{case_id}/inputs/ct"),
        ("get", f"/api/v1/cases/{case_id}/inputs"),
        ("get", f"/api/v1/cases/{case_id}/inputs/ct/download"),
        ("delete", f"/api/v1/cases/{case_id}/inputs/ct"),
        ("post", f"/api/v1/cases/{case_id}/runs"),
        ("get", f"/api/v1/cases/{case_id}/runs"),
        ("get", f"/api/v1/runs/{run_id}"),
        ("get", f"/api/v1/runs/{run_id}/steps"),
        ("get", f"/api/v1/runs/{run_id}/steps/{uuid.uuid4()}/log"),
        ("get", f"/api/v1/runs/{run_id}/provenance"),
        ("get", f"/api/v1/runs/{run_id}/artifacts"),
        ("get", f"/api/v1/runs/{run_id}/artifacts/{uuid.uuid4()}/download"),
        ("get", f"/api/v1/runs/{run_id}/reports"),
        ("get", f"/api/v1/runs/{run_id}/reports/{uuid.uuid4()}/pdf"),
        ("post", f"/api/v1/runs/{run_id}/cancel"),
        ("post", f"/api/v1/runs/{run_id}/resume"),
        ("post", f"/api/v1/runs/{run_id}/retry"),
        ("get", f"/api/v1/pipelines/{PIPE}/versions/1.0.0"),
        ("get", "/api/v1/admin/implementations"),
    ]
    for method, url in calls:
        r = getattr(v1.client, method)(url)
        assert r.status_code == 401, (method, url, r.status_code)
        assert r.json()["code"] == "not_authenticated"


def add_report(v1, run_id):
    """A report row for the run (its PDF is an existing artefact; only ids matter here)."""
    with v1.Session() as db:
        artifact = db.scalars(
            select(Artifact).where(Artifact.pipeline_run_id == uuid.UUID(run_id))
        ).first()
        report = Report(
            case_id=artifact.case_id,
            pipeline_run_id=artifact.pipeline_run_id,
            step_run_id=artifact.step_run_id,
            pdf_artifact_id=artifact.id,
            json_artifact_id=artifact.id,
            template="demo-report",
            language="en",
            content_sha256=artifact.sha256,
        )
        db.add(report)
        db.commit()
        return str(report.id)


def first_step_id(v1, run_id):
    return v1.client.get(f"/api/v1/runs/{run_id}/steps", headers=v1.alice).json()[0][
        "id"
    ]


def test_other_researchers_see_nothing_of_a_case(v1):
    c = v1.client
    v1.enable_all()
    case_id, run_id = v1.finished_run()
    artifact_id = c.get(f"/api/v1/runs/{run_id}/artifacts", headers=v1.alice).json()[0][
        "id"
    ]
    report_id = add_report(v1, run_id)
    step_id = first_step_id(v1, run_id)
    bob = v1.bob
    expect_404 = [
        ("get", f"/api/v1/cases/{case_id}", {}),
        ("patch", f"/api/v1/cases/{case_id}", {"json": {"sex": "male"}}),
        ("delete", f"/api/v1/cases/{case_id}", {}),
        (
            "put",
            f"/api/v1/cases/{case_id}/inputs/ct",
            {"files": {"file": ("a.nii.gz", nifti_gz(), "application/gzip")}},
        ),
        ("get", f"/api/v1/cases/{case_id}/inputs", {}),
        ("get", f"/api/v1/cases/{case_id}/inputs/ct/download", {}),
        ("delete", f"/api/v1/cases/{case_id}/inputs/ct", {}),
        ("post", f"/api/v1/cases/{case_id}/runs", {"json": {"pipeline_id": PIPE}}),
        ("get", f"/api/v1/cases/{case_id}/runs", {}),
        ("get", f"/api/v1/runs/{run_id}", {}),
        ("get", f"/api/v1/runs/{run_id}/steps", {}),
        ("get", f"/api/v1/runs/{run_id}/steps/{uuid.uuid4()}/log", {}),
        ("get", f"/api/v1/runs/{run_id}/provenance", {}),
        ("get", f"/api/v1/runs/{run_id}/artifacts", {}),
        ("get", f"/api/v1/runs/{run_id}/artifacts/{artifact_id}/download", {}),
        ("get", f"/api/v1/runs/{run_id}/reports", {}),
        ("get", f"/api/v1/runs/{run_id}/reports/{report_id}/pdf", {}),
        ("get", f"/api/v1/runs/{run_id}/steps/{step_id}/log", {}),
        ("post", f"/api/v1/runs/{run_id}/cancel", {}),
        ("post", f"/api/v1/runs/{run_id}/resume", {}),
        ("post", f"/api/v1/runs/{run_id}/retry", {}),
    ]
    for method, url, kwargs in expect_404:
        r = getattr(c, method)(url, headers=bob, **kwargs)
        assert r.status_code == 404, (method, url, r.status_code, r.text)
        assert r.json()["code"] in {"case_not_found", "run_not_found"}
    assert c.get("/api/v1/cases", headers=bob).json() == []
    # Nothing changed for the owner.
    assert c.get(f"/api/v1/cases/{case_id}", headers=v1.alice).json()["sex"] is None


def test_admin_acts_on_every_case_and_sees_all(v1):
    c = v1.client
    v1.enable_all()
    case_id, run_id = v1.finished_run()
    assert [x["id"] for x in c.get("/api/v1/cases", headers=v1.admin).json()] == [
        case_id
    ]
    assert c.get(f"/api/v1/cases/{case_id}", headers=v1.admin).status_code == 200
    assert c.get(f"/api/v1/runs/{run_id}", headers=v1.admin).status_code == 200
    assert (
        c.get(
            f"/api/v1/cases/{case_id}/inputs/ct/download", headers=v1.admin
        ).status_code
        == 200
    )
    assert (
        c.get(f"/api/v1/runs/{run_id}/provenance", headers=v1.admin).status_code == 200
    )
    second = v1.new_case(headers=v1.bob)
    assert v1.upload(second, "lesion_mni", headers=v1.admin).status_code == 201
    run = v1.start(second, headers=v1.admin).json()
    assert run["created_by_user_id"] == str(v1.admin_id)
    assert (
        c.post(f"/api/v1/runs/{run['id']}/cancel", headers=v1.admin).status_code == 200
    )
    assert (
        c.post(f"/api/v1/runs/{run['id']}/resume", headers=v1.admin).status_code == 202
    )


def test_keep_forever_is_admin_only_and_blocks_deletion(v1):
    c = v1.client
    case_id = v1.new_case()
    refused = c.patch(
        f"/api/v1/cases/{case_id}", json={"keep_forever": True}, headers=v1.alice
    )
    assert refused.status_code == 403 and refused.json()["code"] == "permission_denied"
    assert (
        c.patch(
            f"/api/v1/cases/{case_id}", json={"keep_forever": True}, headers=v1.admin
        ).json()["keep_forever"]
        is True
    )
    blocked = c.delete(f"/api/v1/cases/{case_id}", headers=v1.alice)
    assert blocked.status_code == 409 and blocked.json()["code"] == "case_keep_forever"
    c.patch(f"/api/v1/cases/{case_id}", json={"keep_forever": False}, headers=v1.admin)
    assert c.delete(f"/api/v1/cases/{case_id}", headers=v1.alice).status_code == 204


def test_reviewer_and_coordinator_have_no_grants_in_phase_0(v1):
    c = v1.client
    v1.enable_all()
    case_id, run_id = v1.finished_run()
    for role in ("reviewer", "coordinator"):
        headers, _ = auth_headers(c, f"{role}@example.org", "secret-pass", role=role)
        # Granted scope, no grants: invisible (404), empty lists.
        assert c.get(f"/api/v1/cases/{case_id}", headers=headers).status_code == 404
        assert c.get(f"/api/v1/runs/{run_id}", headers=headers).status_code == 404
        assert c.get("/api/v1/cases", headers=headers).json() == []
        # Actions the role never has: refused before any lookup.
        assert c.post("/api/v1/cases", json={}, headers=headers).status_code == 403
        assert (
            c.post(
                f"/api/v1/cases/{case_id}/runs",
                json={"pipeline_id": PIPE},
                headers=headers,
            ).status_code
            == 403
        )
        assert (
            c.post(f"/api/v1/runs/{run_id}/cancel", headers=headers).status_code == 403
        )
        assert (
            c.post(f"/api/v1/runs/{run_id}/resume", headers=headers).status_code == 403
        )
        assert (
            c.get(
                f"/api/v1/runs/{run_id}/steps/{uuid.uuid4()}/log", headers=headers
            ).status_code
            == 403
        )
        assert c.delete(f"/api/v1/cases/{case_id}", headers=headers).status_code == 403
        assert (
            c.get("/api/v1/admin/implementations", headers=headers).status_code == 403
        )
        assert c.patch(
            f"/api/v1/cases/{case_id}", json={"keep_forever": True}, headers=headers
        ).status_code in (403, 404)
    reviewer, _ = auth_headers(c, "rev2@example.org", "secret-pass", role="reviewer")
    assert (
        c.get(
            f"/api/v1/cases/{case_id}/inputs/ct/download", headers=reviewer
        ).status_code
        == 404
    )


def test_researcher_cannot_use_admin_endpoints(v1):
    assert (
        v1.client.get("/api/v1/admin/implementations", headers=v1.alice).status_code
        == 403
    )
    url = f"/api/v1/admin/implementations/{PIPE}/intake/intake"
    assert (
        v1.client.put(url, json={"enabled": True}, headers=v1.alice).status_code == 403
    )


def first_step_id_of(v1, run_id):
    return first_step_id(v1, run_id)


def test_artifact_of_another_run_cannot_be_fetched_through_this_run(v1):
    c = v1.client
    v1.enable_all()
    _case_a, run_a = v1.finished_run()
    _case_b, run_b = v1.finished_run()
    art_b = c.get(f"/api/v1/runs/{run_b}/artifacts", headers=v1.alice).json()[0]["id"]
    r = c.get(f"/api/v1/runs/{run_a}/artifacts/{art_b}/download", headers=v1.alice)
    assert r.status_code == 404 and r.json()["code"] == "artifact_not_found"
    report_b = add_report(v1, run_b)
    r = c.get(f"/api/v1/runs/{run_a}/reports/{report_b}/pdf", headers=v1.alice)
    assert r.status_code == 404 and r.json()["code"] == "report_not_found"
    step_b = first_step_id_of(v1, run_b)
    r = c.get(f"/api/v1/runs/{run_a}/steps/{step_b}/log", headers=v1.alice)
    assert r.status_code == 404 and r.json()["code"] == "step_run_not_found"


def test_legacy_endpoints_are_untouched(v1):
    from tests.test_api import upload_test_study

    c = v1.client
    study_id = upload_test_study(c, v1.alice)
    assert c.get(f"/api/studies/{study_id}/status", headers=v1.alice).status_code == 200
    assert (
        c.get(f"/api/studies/{study_id}", headers=v1.alice).json()["status"] == "queued"
    )
    assert [x["id"] for x in c.get("/api/studies", headers=v1.alice).json()] == [
        str(study_id)
    ]
    assert c.get(f"/api/studies/{study_id}/status", headers=v1.bob).status_code == 404
    # The legacy upload creates no case and is invisible to v1.
    assert c.get("/api/v1/cases", headers=v1.alice).json() == []
    assert c.get(f"/api/v1/cases/{study_id}", headers=v1.alice).status_code == 404
    with v1.Session() as db:
        assert db.scalar(select(Case)) is None


# ------------------------------------------------------------------- OpenAPI


def test_openapi_v1_is_served_and_snapshot_is_current(v1):
    served = v1.client.get("/api/v1/openapi.json")
    assert served.status_code == 200
    assert served.json() == build_openapi(app)
    assert v1.client.get("/api/v1/docs").status_code == 200
    schema = served.json()
    assert schema["info"]["title"] == "Stroke Server API v1"
    assert all(path.startswith("/api/v1/") for path in schema["paths"])
    assert "/api/v1/cases/{case_id}/runs" in schema["paths"]
    assert "/api/studies" not in schema["paths"]

    assert OPENAPI_SNAPSHOT.is_file(), "missing docs/api/openapi-v1.json"
    committed = json.loads(OPENAPI_SNAPSHOT.read_text(encoding="utf-8"))
    assert committed == json.loads(json.dumps(schema)), (
        "docs/api/openapi-v1.json is out of date; regenerate with "
        "`PYTHONPATH=.:backend python -m app.api.v1.openapi docs/api/openapi-v1.json`"
    )
