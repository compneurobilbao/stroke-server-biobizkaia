"""Legacy cut-over (docs/data-model.md 10.1 stage 2, 10.4): the legacy endpoints are
aliases over the case model and the legacy worker task writes it directly.

The end-to-end tests run the real ``process_study`` task synchronously (the Celery
``delay`` of the upload and retry aliases is replaced by a direct call) with the dummy
processor, so they need a POSIX system, like the rest of the worker tests. They replace
the dual-write (mirror) tests of F0-15: nothing writes ``studies`` or
``processing_jobs`` any more.
"""

from __future__ import annotations

import re
import sys
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models import (
    Artifact,
    AuditEvent,
    Case,
    InputFile,
    Notification,
    PipelineRun,
    ProcessingJob,
    Report,
    RunSlot,
    ShareLink,
    StepRun,
    Study,
    User,
)
from app.services import legacy_studies
from app.services.quota import storage_used
from processor_adapter.cancellation import ProcessorCanceled
from tests.legacy_state import study_dir
from tests.test_api import auth_headers, upload_test_study

NAIVE_ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?$")


class QueuedTask:
    """Stands in for the Celery task: ``delay`` queues the call, ``run_pending`` runs
    the real task in-process afterwards (in the main thread, where the worker installs
    its signal handlers, as a Celery worker process does)."""

    def __init__(self):
        self.revoked: list[tuple[str, dict]] = []
        self.pending: list[tuple[str, str]] = []
        self.calls = 0
        outer = self

        class _Control:
            @staticmethod
            def revoke(task_id, **kwargs):
                outer.revoked.append((task_id, kwargs))

        self.app = type("FakeApp", (), {"control": _Control})()

    def delay(self, study_id, job_id):
        self.calls += 1
        self.pending.append((study_id, job_id))
        return type("Result", (), {"id": f"celery-{self.calls}"})()

    def run_pending(self):
        from worker.tasks import process_study

        while self.pending:
            process_study.run(*self.pending.pop(0))


@pytest.fixture()
def legacy(client, monkeypatch):
    """The legacy API with the real worker task behind it."""
    import app.api.studies as routes
    import worker.tasks as tasks

    def use_processor(fail: bool = False):
        flags = " --fail" if fail else ""
        monkeypatch.setenv(
            "PROCESSOR_COMMAND",
            f"{sys.executable} external_processor/dummy_processor.py"
            f" --input {{input_dir}} --output {{output_dir}} --study-id {{study_id}}"
            f" --sleep 0{flags}",
        )
        get_settings.cache_clear()

    task = QueuedTask()
    monkeypatch.setattr(routes, "process_study", task)
    monkeypatch.setattr(tasks, "SessionLocal", client.app.state.testing_session_local)
    use_processor()
    headers, user_id = auth_headers(client, "researcher@example.org", "secret-pass")
    return type(
        "LegacyEnv",
        (),
        {
            "client": client,
            "Session": client.app.state.testing_session_local,
            "headers": headers,
            "user_id": user_id,
            "task": task,
            "use_processor": staticmethod(use_processor),
            "upload": staticmethod(lambda: _upload_and_process(client, headers, task)),
        },
    )


def _upload_and_process(client, headers, task) -> uuid.UUID:
    study_id = upload_test_study(client, headers)
    task.run_pending()
    return study_id


def legacy_tables_empty(Session) -> bool:
    with Session() as db:
        return (
            db.scalar(select(func.count()).select_from(Study)) == 0
            and db.scalar(select(func.count()).select_from(ProcessingJob)) == 0
        )


# ------------------------------------------------------------- direct write


def test_upload_process_download_writes_only_the_case_model(legacy):
    c, h = legacy.client, legacy.headers
    study_id = legacy.upload()

    status = c.get(f"/api/studies/{study_id}/status", headers=h).json()
    assert status["status"] == "completed" and status["has_pdf"] is True
    pdf = c.get(f"/api/studies/{study_id}/download/pdf", headers=h)
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert pdf.headers["content-disposition"].endswith(
        f'filename="technical-report-{study_id}.pdf"'
    )

    with legacy.Session() as db:
        case = db.get(Case, study_id)
        assert case.deployment_profile == "legacy" and case.legacy_study_id is None
        assert case.legacy_attributes["original_filename"] == "study.nii"
        upload = db.get(InputFile, legacy_studies.derive(study_id, "input"))
        assert upload.role == "legacy_upload" and upload.size_bytes == len(b"dummy image")
        assert not upload.storage_path.startswith("/")
        run = db.get(PipelineRun, study_id)
        assert (run.pipeline_id, run.status, run.input_set) == (
            "legacy",
            "succeeded",
            "legacy-single-file",
        )
        assert run.started_at is not None and run.finished_at is not None
        slot = db.get(RunSlot, legacy_studies.derive(study_id, "slot/legacy"))
        assert (slot.slot, slot.implementation_id, slot.stub) == ("legacy", "dummy", True)
        (step,) = db.scalars(select(StepRun).where(StepRun.pipeline_run_id == study_id))
        assert (step.attempt, step.trigger, step.status, step.outcome) == (
            1,
            "initial",
            "succeeded",
            "succeeded",
        )
        assert step.executor == "subprocess" and step.legacy_job_id is None
        assert step.orchestrator_task_id == "celery-1"
        assert step.log_path and not step.log_path.startswith("/")
        pdf_artifact = db.get(
            Artifact, legacy_studies.derive(study_id, "artifact/technical_report")
        )
        assert pdf_artifact.step_run_id == step.id and pdf_artifact.origin == "legacy"
        assert pdf_artifact.size_bytes == len(pdf.content)
        assert re.fullmatch(r"[0-9a-f]{64}", pdf_artifact.sha256)
        report = db.get(Report, legacy_studies.derive(study_id, "report"))
        assert report.template == "legacy-technical"
        assert report.pdf_artifact_id == pdf_artifact.id
        events = {
            e.event_type: e
            for e in db.scalars(select(AuditEvent).where(AuditEvent.case_id == study_id))
        }
        assert {"study_uploaded", "processing_started", "processing_completed"} <= set(
            events
        )
        assert all(e.study_id is None for e in events.values())
        assert events["processing_started"].pipeline_run_id == study_id
        notification = db.scalar(select(Notification))
        assert notification.event_type == "processing_completed"
        assert notification.case_id == study_id and notification.study_id is None
        # The quota counts the upload and the stored artefacts, everywhere.
        used = storage_used(db, legacy.user_id)
        assert used == upload.size_bytes + pdf_artifact.size_bytes
    me = c.get("/api/auth/me", headers=h).json()
    assert me["storage_used_bytes"] == used
    # The notification keeps its legacy study id (legacy study id = case id).
    listed = c.get("/api/notifications", headers=h).json()
    assert [n["study_id"] for n in listed] == [str(study_id)]
    assert legacy_tables_empty(legacy.Session)


def test_failure_then_retry_adds_an_attempt_of_the_same_run(legacy):
    c, h = legacy.client, legacy.headers
    legacy.use_processor(fail=True)
    study_id = legacy.upload()
    failed = c.get(f"/api/studies/{study_id}", headers=h).json()
    assert failed["status"] == "failed"
    assert failed["error_message"] == "The external processor finished with an error"
    with legacy.Session() as db:
        run = db.get(PipelineRun, study_id)
        assert run.failure_code == "legacy_failure"

    legacy.use_processor(fail=False)
    retry = c.post(f"/api/studies/{study_id}/retry", headers=h)
    legacy.task.run_pending()
    assert retry.status_code == 200
    assert retry.json() == {
        "id": str(study_id),
        "status": "queued",
        "message": "Job requeued",
    }
    detail = c.get(f"/api/studies/{study_id}/detail", headers=h).json()
    assert detail["status"] == "completed" and detail["has_pdf"] is True
    assert detail["error_message"] is None
    assert [j["status"] for j in detail["jobs"]] == ["completed", "failed"]
    assert [j["retry_count"] for j in detail["jobs"]] == [1, 0]
    with legacy.Session() as db:
        run = db.get(PipelineRun, study_id)
        assert run.status == "succeeded" and run.resume_count == 1
        steps = db.scalars(
            select(StepRun)
            .where(StepRun.pipeline_run_id == study_id)
            .order_by(StepRun.attempt)
        ).all()
        assert [(s.attempt, s.trigger, s.status) for s in steps] == [
            (1, "initial", "failed"),
            (2, "user_resume", "succeeded"),
        ]
        pdf = db.get(Artifact, legacy_studies.derive(study_id, "artifact/technical_report"))
        assert pdf.step_run_id == steps[1].id
    assert c.post(f"/api/studies/{study_id}/retry", headers=h).status_code == 409
    assert legacy_tables_empty(legacy.Session)


def test_cancel_while_processing_revokes_with_sigterm_and_cancels(legacy, monkeypatch):
    """The legacy cancellation path end to end: the alias sets the flag on the run and
    on the live attempt and revokes the Celery task with SIGTERM; the worker's signal
    handler raises ProcessorCanceled, and the flag turns it into a cancellation."""
    import worker.tasks as tasks

    c, h = legacy.client, legacy.headers
    seen = {}

    class CancelledInFlight:
        @staticmethod
        def run(study_id, **_kwargs):
            response = c.post(f"/api/studies/{study_id}/cancel", headers=h)
            seen["cancel"] = response.json()
            raise ProcessorCanceled("Processing canceled by signal 15")

    monkeypatch.setattr(
        tasks, "create_processor_adapter", lambda **_kwargs: CancelledInFlight()
    )
    study_id = legacy.upload()

    assert seen["cancel"] == {
        "id": str(study_id),
        "status": "processing",
        "message": "Cancellation requested",
    }
    assert legacy.task.revoked == [
        ("celery-1", {"terminate": True, "signal": "SIGTERM"})
    ]
    study = c.get(f"/api/studies/{study_id}", headers=h).json()
    assert study["status"] == "canceled"
    assert study["error_message"] == "Canceled by the user"
    assert study["has_pdf"] is False
    with legacy.Session() as db:
        step = db.scalar(select(StepRun).where(StepRun.pipeline_run_id == study_id))
        assert (step.status, step.outcome) == ("canceled", "canceled")
        assert step.failure_message == "Processing canceled by signal 15"
        assert db.scalar(select(func.count()).select_from(Notification)) == 0
    assert c.post(f"/api/studies/{study_id}/cancel", headers=h).status_code == 409


def test_share_link_binds_the_report_and_serves_it(legacy):
    c, h = legacy.client, legacy.headers
    study_id = legacy.upload()
    created = c.post(f"/api/studies/{study_id}/share-links", headers=h)
    assert created.status_code == 201
    assert created.json()["study_id"] == str(study_id)
    with legacy.Session() as db:
        link = db.scalar(select(ShareLink))
        assert link.case_id == study_id and link.study_id is None
        assert link.report_id == legacy_studies.derive(study_id, "report")
    download = c.get(urlparse(created.json()["url"]).path)
    assert download.status_code == 200 and download.content.startswith(b"%PDF")
    assert download.headers.get("deprecation") == "true"


# ------------------------------------------------------------- alias semantics


def test_responses_keep_the_legacy_shape_and_announce_the_successor(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    study_id = upload_test_study(client, headers)

    listing = client.get("/api/studies", headers=headers)
    assert listing.headers["deprecation"] == "true"
    assert listing.headers["link"] == '</api/v1/cases>; rel="successor-version"'
    study = client.get(f"/api/studies/{study_id}", headers=headers)
    assert (
        study.headers["link"] == f'</api/v1/cases/{study_id}>; rel="successor-version"'
    )
    body = study.json()
    assert set(body) == {
        "id",
        "owner_user_id",
        "original_filename",
        "status",
        "created_at",
        "updated_at",
        "processing_started_at",
        "processing_finished_at",
        "deleted_at",
        "keep_forever",
        "error_message",
        "processor_name",
        "processor_version",
        "processor_backend",
        "bids_subject_id",
        "file_size",
        "checksum",
        "has_pdf",
        "has_output_zip",
        "processing_warnings",
        "clinical_review_status",
    }
    # Legacy timestamps were naive UTC; the alias keeps that representation.
    assert NAIVE_ISO.match(body["created_at"]) and NAIVE_ISO.match(body["updated_at"])
    job = client.get(f"/api/studies/{study_id}/detail", headers=headers).json()["jobs"][0]
    assert NAIVE_ISO.match(job["queued_at"])
    assert job["study_id"] == str(study_id) and job["retry_count"] == 0
    assert client.get("/api/health").headers.get("deprecation") is None
    assert client.get("/api/auth/me", headers=headers).headers.get("deprecation") is None


def test_v1_cases_are_not_served_by_the_aliases(client):
    """The legacy endpoints serve only legacy-profile cases, as /api/v1 serves none."""
    from tests.test_api_v1 import V1Env

    v1 = V1Env(client)
    v1.enable_all()
    case_id = v1.native_case()
    with v1.Session() as db:
        db.get(Case, uuid.UUID(case_id)).keep_forever = True
        db.commit()
    assert client.get("/api/studies", headers=v1.alice).json() == []
    assert client.get("/api/studies", headers=v1.admin).json() == []
    for method, path in (
        ("get", ""),
        ("get", "/status"),
        ("get", "/detail"),
        ("get", "/logs"),
        ("get", "/download/pdf"),
        ("post", "/cancel"),
        ("post", "/retry"),
        ("post", "/share-links"),
        ("get", "/share-links"),
        ("delete", ""),
    ):
        response = getattr(client, method)(
            f"/api/studies/{case_id}{path}", headers=v1.admin
        )
        assert response.status_code == 404, (method, path, response.text)
        assert response.json()["code"] == "study_not_found"
    with v1.Session() as db:
        assert db.get(Case, uuid.UUID(case_id)).deleted_at is None

    # A finished v1 run does not count as a legacy job on the dashboard.
    v1.finished_run()
    from app.api import admin as admin_routes

    with v1.Session() as db:
        assert admin_routes._jobs_by_status(db) == {}
        assert admin_routes._studies_by_status(db) == {}


def test_cancel_that_loses_the_race_to_the_worker_cancels_the_running_attempt(
    legacy, monkeypatch
):
    """The run is queued when the cancel reads it, but the worker starts it before the
    queued->canceled compare-and-set: the cancel continues as a processing cancel."""
    c, h = legacy.client, legacy.headers
    study_id = upload_test_study(c, h)  # queued; the task is not run
    real_latest_step = legacy_studies.latest_step
    calls = []

    def worker_starts_now(db, run):
        if not calls:
            calls.append(run.id)
            with legacy.Session() as other:  # what process_study's start commits
                started = datetime.now(timezone.utc)
                r = other.get(PipelineRun, run.id)
                r.status, r.started_at = "running", started
                step = other.scalar(
                    select(StepRun).where(StepRun.pipeline_run_id == run.id)
                )
                step.status, step.started_at = "running", started
                step.orchestrator_task_id = "celery-live"
                other.commit()
        return real_latest_step(db, run)

    monkeypatch.setattr(legacy_studies, "latest_step", worker_starts_now)
    response = c.post(f"/api/studies/{study_id}/cancel", headers=h)

    assert response.status_code == 200
    assert response.json() == {
        "id": str(study_id),
        "status": "processing",
        "message": "Cancellation requested",
    }
    assert ("celery-live", {"terminate": True, "signal": "SIGTERM"}) in legacy.task.revoked
    with legacy.Session() as db:
        run = db.get(PipelineRun, study_id)
        step = db.scalar(select(StepRun).where(StepRun.pipeline_run_id == study_id))
        assert run.status == "running" and run.cancel_requested_at is not None
        assert step.status == "running" and step.cancel_requested_at is not None


def test_dashboard_ignores_attempts_closed_by_a_retry(legacy, monkeypatch):
    """An attempt left live by a crashed worker is closed as stale by the retry; it is
    not a failure to review."""
    c, h = legacy.client, legacy.headers
    admin, _ = auth_headers(c, "admin@example.org", "secret-pass", role="admin")
    study_id = upload_test_study(c, h)
    with legacy.Session() as db:  # failed run whose attempt still looks live
        run = db.get(PipelineRun, study_id)
        run.status, run.finished_at = "failed", datetime.now(timezone.utc)
        step = db.scalar(select(StepRun).where(StepRun.pipeline_run_id == study_id))
        step.status = "running"
        db.commit()
    assert c.post(f"/api/studies/{study_id}/retry", headers=h).status_code == 200
    from app.api import admin as admin_routes

    with legacy.Session() as db:
        closed = db.scalar(
            select(StepRun).where(
                StepRun.pipeline_run_id == study_id, StepRun.attempt == 1
            )
        )
        assert closed.failure_code == "legacy_stale_job"
        assert admin_routes._jobs_by_status(db) == {"failed": 1, "queued": 1}
    monkeypatch.setattr(admin_routes, "_inspect_worker_stats", lambda: ({}, None))
    dashboard = c.get("/api/admin/dashboard", headers=admin).json()
    assert dashboard["recent_failed_jobs"] == []


def test_backfilled_study_through_the_aliases(client):
    """A study back-filled by migration 0011: the alias reads the new rows, keeps the
    study id on new share links (so a downgrade keeps them) and never writes the
    frozen legacy rows."""
    headers, user_id = auth_headers(client, "researcher@example.org", "secret-pass")
    Session = client.app.state.testing_session_local
    sid = uuid.uuid4()
    root = study_dir(sid).parent
    pdf = study_dir(sid) / "output" / "report.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    then = datetime(2026, 3, 1, 8, 0, 0)
    with Session() as db:
        db.add(
            Study(
                id=sid,
                owner_user_id=user_id,
                original_filename="old.nii.gz",
                stored_path=str(study_dir(sid) / "input" / "old.nii.gz"),
                pdf_path=str(pdf),
                status="completed",
                created_at=then,
                updated_at=then,
            )
        )
        db.flush()
        job_id = uuid.uuid4()
        db.add(ProcessingJob(id=job_id, study_id=sid, status="completed", queued_at=then))
        db.flush()
        from tests.legacy_state import RUN_STATUS  # noqa: F401  (documents the mapping)

        aware = then.replace(tzinfo=timezone.utc)
        db.add(
            Case(
                id=sid,
                owner_user_id=user_id,
                subject_label=sid.hex[:12],
                deployment_profile="legacy",
                created_at=aware,
                updated_at=aware,
                legacy_study_id=sid,
                legacy_attributes={"original_filename": "old.nii.gz"},
            )
        )
        db.flush()
        db.add(
            InputFile(
                id=legacy_studies.derive(sid, "input"),
                case_id=sid,
                role="legacy_upload",
                artefact_type="legacy_upload",
                storage_path=f"{sid}/input/old.nii.gz",
                uploaded_by_user_id=user_id,
                created_at=aware,
            )
        )
        db.add(
            PipelineRun(
                id=sid,
                case_id=sid,
                created_by_user_id=user_id,
                pipeline_id="legacy",
                pipeline_version="0.0.0",
                platform_version="legacy",
                artefact_types_version="legacy",
                input_set="legacy-single-file",
                seed=0,
                status="succeeded",
                created_at=aware,
                updated_at=aware,
                finished_at=aware,
                requested_implementations={},
                accounting_submitted_by=f"user:{user_id}",
                accounting_submit_as="service-account",
                legacy_clinical_review_status="technical_only",
            )
        )
        db.flush()
        slot_id = legacy_studies.derive(sid, "slot/legacy")
        db.add(
            RunSlot(
                id=slot_id,
                pipeline_run_id=sid,
                slot="legacy",
                position=0,
                optional=False,
                status="selected",
                implementation_id="unknown",
                step_name="legacy-unknown",
                step_version="0.0.0",
                stub=False,
                chosen_by="deployment_default",
                effective_parameters={},
            )
        )
        db.flush()
        db.add(
            StepRun(
                id=job_id,
                pipeline_run_id=sid,
                run_slot_id=slot_id,
                attempt=1,
                trigger="initial",
                status="succeeded",
                outcome="succeeded",
                executor="subprocess",
                runtime="none",
                queued_at=aware,
                finished_at=aware,
                legacy_job_id=job_id,
            )
        )
        db.flush()
        art_id = legacy_studies.derive(sid, "artifact/technical_report")
        db.add(
            Artifact(
                id=art_id,
                case_id=sid,
                pipeline_run_id=sid,
                step_run_id=job_id,
                origin="legacy",
                port="technical_report",
                artefact_type="legacy_technical_report_pdf",
                storage_path=pdf.relative_to(root).as_posix(),
                synthetic=False,
                created_at=aware,
            )
        )
        db.flush()
        db.add(
            Report(
                id=legacy_studies.derive(sid, "report"),
                case_id=sid,
                pipeline_run_id=sid,
                step_run_id=job_id,
                pdf_artifact_id=art_id,
                template="legacy-technical",
                language="es",
                created_at=aware,
            )
        )
        db.commit()

    body = client.get(f"/api/studies/{sid}", headers=headers).json()
    assert body["status"] == "completed" and body["has_pdf"] is True
    assert body["original_filename"] == "old.nii.gz"
    assert body["created_at"] == "2026-03-01T08:00:00"
    review = client.patch(
        f"/api/studies/{sid}/clinical-review",
        json={"clinical_review_status": "reviewed"},
        headers=headers,
    )
    assert review.json()["clinical_review_status"] == "reviewed"
    created = client.post(f"/api/studies/{sid}/share-links", headers=headers)
    assert created.status_code == 201
    with Session() as db:
        link = db.scalar(select(ShareLink))
        assert link.study_id == sid and link.case_id == sid
        # the frozen legacy rows are untouched
        study = db.get(Study, sid)
        assert study.clinical_review_status == "technical_only"
        assert study.updated_at == then
        assert db.get(PipelineRun, sid).legacy_clinical_review_status == "reviewed"
    deleted = client.delete(f"/api/studies/{sid}", headers=headers)
    assert deleted.status_code == 200 and deleted.json()["status"] == "completed"
    with Session() as db:
        assert db.get(Study, sid).deleted_at is None
        assert db.get(Case, sid).deleted_at is not None


def test_quota_is_the_same_figure_for_legacy_and_v1(client):
    headers, user_id = auth_headers(client, "researcher@example.org", "secret-pass")
    admin, _ = auth_headers(client, "admin@example.org", "secret-pass", role="admin")
    upload_test_study(client, headers)
    upload_test_study(client, headers)
    with client.app.state.testing_session_local() as db:
        expected = storage_used(db, user_id)
        db.get(User, user_id).storage_quota_bytes = expected + len(b"dummy image") - 1
        db.commit()
    assert expected == 2 * len(b"dummy image")
    users = {u["id"]: u for u in client.get("/api/users", headers=admin).json()}
    assert users[str(user_id)]["storage_used_bytes"] == expected
    over = client.post(
        "/api/studies/upload",
        files={"file": ("third.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    assert over.status_code == 413 and over.json()["code"] == "storage_quota_exceeded"
    assert len(client.get("/api/studies", headers=headers).json()) == 2
