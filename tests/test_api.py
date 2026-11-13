from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID

from app.models.audit_event import AuditEvent
from app.models.notification import Notification
from app.models.processing_job import ProcessingJob
from app.models.share_link import ShareLink
from app.models.study import Study, StudyStatus
from app.models.user import User, UserRole
from app.schemas.admin import AdminServiceHealth
from app.cli import retention as retention_cli
from app.services.auth import hash_password, normalize_email
from app.services.retention import apply_retention_policy
from app.services.storage import LocalStudyStorage


def test_healthcheck(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_login_returns_token_and_records_audit_event(client):
    create_test_user(
        client, "admin@example.org", "secret-pass", role=UserRole.admin.value
    )

    response = client.post(
        "/api/auth/login",
        json={"email": "admin@example.org", "password": "secret-pass"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["token_type"] == "bearer"
    assert payload["access_token"]
    assert payload["user"]["role"] == "admin"

    db = client.app.state.testing_session_local()
    event = (
        db.query(AuditEvent).filter(AuditEvent.event_type == "login_succeeded").one()
    )
    assert event.actor == "admin@example.org"
    assert event.actor_user_id is not None
    db.close()


def test_login_rejects_invalid_password(client):
    create_test_user(client, "researcher@example.org", "secret-pass")

    response = client.post(
        "/api/auth/login",
        json={"email": "researcher@example.org", "password": "bad-pass"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Credenciales no válidas"


def test_unauthenticated_user_cannot_access_protected_studies(client):
    response = client.get("/api/studies")

    assert response.status_code == 401


def test_upload_creates_owned_study_and_audit_event(client):
    headers, user_id = auth_headers(client, "researcher@example.org", "secret-pass")

    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "queued"

    list_response = client.get("/api/studies", headers=headers)
    assert list_response.status_code == 200
    studies = list_response.json()
    assert len(studies) == 1
    assert studies[0]["original_filename"] == "study.nii"
    assert studies[0]["owner_user_id"] == str(user_id)
    assert studies[0]["has_output_zip"] is False
    assert studies[0]["clinical_review_status"] == "technical_only"

    db = client.app.state.testing_session_local()
    event = db.query(AuditEvent).filter(AuditEvent.event_type == "study_uploaded").one()
    assert event.actor_user_id == user_id
    db.close()


def test_upload_rejects_invalid_extension(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")

    response = client.post(
        "/api/studies/upload",
        files={"file": ("patient.exe", b"bad", "application/octet-stream")},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Extensión de fichero no permitida"


def test_upload_accepts_optional_bids_subject_id(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")

    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii.gz", b"dummy image", "application/octet-stream")},
        data={"bids_subject_id": "sub-O01"},
        headers=headers,
    )

    assert response.status_code == 201
    assert response.json()["bids_subject_id"] == "sub-O01"

    studies = client.get("/api/studies", headers=headers).json()
    assert studies[0]["bids_subject_id"] == "sub-O01"


def test_researcher_only_sees_own_studies_and_admin_sees_all(client):
    researcher_headers, _ = auth_headers(
        client, "researcher@example.org", "secret-pass"
    )
    other_headers, _ = auth_headers(client, "other@example.org", "secret-pass")
    admin_headers, _ = auth_headers(
        client, "admin@example.org", "secret-pass", role=UserRole.admin.value
    )

    client.post(
        "/api/studies/upload",
        files={"file": ("own.nii", b"dummy image", "application/octet-stream")},
        headers=researcher_headers,
    )
    client.post(
        "/api/studies/upload",
        files={"file": ("other.nii", b"dummy image", "application/octet-stream")},
        headers=other_headers,
    )

    own_studies = client.get("/api/studies", headers=researcher_headers).json()
    admin_studies = client.get("/api/studies", headers=admin_headers).json()

    assert [study["original_filename"] for study in own_studies] == ["own.nii"]
    assert {study["original_filename"] for study in admin_studies} == {
        "own.nii",
        "other.nii",
    }


def test_non_owner_cannot_view_or_download_study(client):
    owner_headers, _ = auth_headers(client, "owner@example.org", "secret-pass")
    other_headers, _ = auth_headers(client, "other@example.org", "secret-pass")

    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=owner_headers,
    )
    study_id = response.json()["id"]

    db = client.app.state.testing_session_local()
    study = db.get(Study, UUID(study_id))
    report_path = Path(study.output_path) / "reports" / "technical_report.pdf"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    study.status = StudyStatus.completed
    study.pdf_path = str(report_path)
    db.commit()
    db.close()

    detail = client.get(f"/api/studies/{study_id}", headers=other_headers)
    download = client.get(
        f"/api/studies/{study_id}/download/pdf", headers=other_headers
    )

    assert detail.status_code == 404
    assert download.status_code == 404


def test_owner_can_download_completed_report(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    study_id = response.json()["id"]

    db = client.app.state.testing_session_local()
    study = db.get(Study, UUID(study_id))
    report_path = Path(study.output_path) / "reports" / "technical_report.pdf"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    study.status = StudyStatus.completed
    study.pdf_path = str(report_path)
    db.commit()
    db.close()

    download = client.get(f"/api/studies/{study_id}/download/pdf", headers=headers)

    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"


def test_owner_can_create_and_use_share_link(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    study_id = response.json()["id"]
    complete_study_with_pdf(client, study_id)

    created = client.post(f"/api/studies/{study_id}/share-links", headers=headers)

    assert created.status_code == 201
    payload = created.json()
    assert payload["url"].endswith("/pdf")
    assert payload["is_revoked"] is False
    assert payload["access_count"] == 0

    download = client.get(urlparse(payload["url"]).path)

    assert download.status_code == 200
    assert download.headers["content-type"] == "application/pdf"

    links = client.get(f"/api/studies/{study_id}/share-links", headers=headers).json()
    assert links[0]["access_count"] == 1
    assert links[0]["last_accessed_at"] is not None

    token = payload["url"].split("/share/", 1)[1].split("/", 1)[0]
    db = client.app.state.testing_session_local()
    link = db.query(ShareLink).one()
    events = {event.event_type for event in db.query(AuditEvent).all()}
    assert link.token_hash != token
    assert "share_link_created" in events
    assert "share_link_pdf_downloaded" in events
    db.close()


def test_non_owner_cannot_create_share_link(client):
    owner_headers, _ = auth_headers(client, "owner@example.org", "secret-pass")
    other_headers, _ = auth_headers(client, "other@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=owner_headers,
    )
    study_id = response.json()["id"]
    complete_study_with_pdf(client, study_id)

    created = client.post(f"/api/studies/{study_id}/share-links", headers=other_headers)

    assert created.status_code == 404


def test_share_link_requires_completed_pdf(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    study_id = response.json()["id"]

    created = client.post(f"/api/studies/{study_id}/share-links", headers=headers)

    assert created.status_code == 409


def test_revoked_share_link_cannot_download_pdf(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    study_id = response.json()["id"]
    complete_study_with_pdf(client, study_id)
    created = client.post(
        f"/api/studies/{study_id}/share-links", headers=headers
    ).json()

    revoked = client.post(
        f"/api/studies/{study_id}/share-links/{created['id']}/revoke", headers=headers
    )
    download = client.get(urlparse(created["url"]).path)

    assert revoked.status_code == 200
    assert revoked.json()["is_revoked"] is True
    assert download.status_code == 404


def test_expired_share_link_cannot_download_pdf(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    study_id = response.json()["id"]
    complete_study_with_pdf(client, study_id)
    created = client.post(
        f"/api/studies/{study_id}/share-links", headers=headers
    ).json()

    db = client.app.state.testing_session_local()
    link = db.get(ShareLink, UUID(created["id"]))
    link.expires_at = datetime.utcnow() - timedelta(hours=1)
    db.commit()
    db.close()

    download = client.get(urlparse(created["url"]).path)

    assert download.status_code == 404


def test_study_detail_includes_jobs(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    study_id = response.json()["id"]

    detail = client.get(f"/api/studies/{study_id}/detail", headers=headers)

    assert detail.status_code == 200
    assert detail.json()["id"] == study_id
    assert detail.json()["jobs"][0]["status"] == "queued"


def test_logs_endpoint_returns_truncated_logs(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    study_id = response.json()["id"]

    db = client.app.state.testing_session_local()
    study = db.get(Study, UUID(study_id))
    log_path = Path(study.output_path).parent / "logs" / "processor.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("line1\nline2\nline3\n", encoding="utf-8")
    db.close()

    logs = client.get(f"/api/studies/{study_id}/logs?lines=2", headers=headers)

    assert logs.status_code == 200
    payload = logs.json()
    assert payload["logs"][0]["name"] == "processor.log"
    assert payload["logs"][0]["content"] == "line2\nline3"
    assert payload["logs"][0]["truncated"] is True


def test_cancel_queued_study(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    study_id = response.json()["id"]

    cancel = client.post(f"/api/studies/{study_id}/cancel", headers=headers)

    assert cancel.status_code == 200
    assert cancel.json()["status"] == "canceled"
    status_response = client.get(f"/api/studies/{study_id}/status", headers=headers)
    assert status_response.json()["status"] == "canceled"

    db = client.app.state.testing_session_local()
    job = db.get(Study, UUID(study_id)).jobs[0]
    assert job.status == StudyStatus.canceled.value
    assert job.cancel_requested_at is None
    db.close()


def test_cancel_processing_study_terminates_celery_task(client, monkeypatch):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    study_id = response.json()["id"]

    db = client.app.state.testing_session_local()
    study = db.get(Study, UUID(study_id))
    job = study.jobs[0]
    study.status = StudyStatus.processing
    job.status = StudyStatus.processing.value
    job.celery_task_id = "celery-task-id"
    db.commit()
    db.close()

    revoked = []

    class FakeControl:
        @staticmethod
        def revoke(task_id, **kwargs):
            revoked.append((task_id, kwargs))

    class FakeTask:
        app = type("FakeApp", (), {"control": FakeControl})()

    import app.api.routes as routes

    monkeypatch.setattr(routes, "process_study", FakeTask)

    cancel = client.post(f"/api/studies/{study_id}/cancel", headers=headers)

    assert cancel.status_code == 200
    assert cancel.json()["status"] == "processing"
    assert cancel.json()["message"] == "Cancelación solicitada"
    assert revoked == [
        ("celery-task-id", {"terminate": True, "signal": "SIGTERM"})
    ]

    db = client.app.state.testing_session_local()
    study = db.get(Study, UUID(study_id))
    job = study.jobs[0]
    assert study.status == StudyStatus.processing
    assert study.error_message == "Cancelación solicitada por el usuario"
    assert job.status == StudyStatus.processing.value
    assert job.error_message == "Cancelación solicitada por el usuario"
    assert job.cancel_requested_at is not None
    db.close()


def test_retry_failed_study_creates_new_job(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    study_id = response.json()["id"]

    db = client.app.state.testing_session_local()
    study = db.get(Study, UUID(study_id))
    study.status = StudyStatus.failed
    study.error_message = "forced failure"
    db.commit()
    db.close()

    retry = client.post(f"/api/studies/{study_id}/retry", headers=headers)

    assert retry.status_code == 200
    assert retry.json()["status"] == "queued"
    detail = client.get(f"/api/studies/{study_id}/detail", headers=headers).json()
    assert len(detail["jobs"]) == 2
    assert detail["jobs"][0]["retry_count"] == 1


def test_delete_study_soft_deletes_and_removes_storage(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    study_id = response.json()["id"]

    db = client.app.state.testing_session_local()
    study = db.get(Study, UUID(study_id))
    study_dir = Path(study.output_path).parent
    assert study_dir.exists()
    db.close()

    delete = client.delete(f"/api/studies/{study_id}", headers=headers)

    assert delete.status_code == 200
    assert not study_dir.exists()
    assert client.get("/api/studies", headers=headers).json() == []

    db = client.app.state.testing_session_local()
    deleted_study = db.get(Study, UUID(study_id))
    event = db.query(AuditEvent).filter(AuditEvent.event_type == "study_deleted").one()
    assert deleted_study.deleted_at is not None
    assert event.study_id == UUID(study_id)
    db.close()


def test_delete_processing_study_is_rejected(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    study_id = response.json()["id"]

    db = client.app.state.testing_session_local()
    study = db.get(Study, UUID(study_id))
    study.status = StudyStatus.processing
    db.commit()
    db.close()

    delete = client.delete(f"/api/studies/{study_id}", headers=headers)

    assert delete.status_code == 409


def test_retention_dry_run_does_not_delete_study(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    study_id = UUID(response.json()["id"])

    db = client.app.state.testing_session_local()
    study = db.get(Study, study_id)
    study.status = StudyStatus.completed
    study.created_at = datetime.utcnow() - timedelta(days=120)
    study_dir = Path(study.output_path).parent
    storage = LocalStudyStorage(study_dir.parent)
    db.commit()

    result = apply_retention_policy(db, storage, 90, dry_run=True)

    assert [candidate.id for candidate in result.candidates] == [study_id]
    assert result.deleted == []
    assert db.get(Study, study_id).deleted_at is None
    assert study_dir.exists()
    db.close()


def test_retention_execute_deletes_only_expired_terminal_studies(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")
    old_id = upload_test_study(client, headers, "old.nii")
    protected_id = upload_test_study(client, headers, "protected.nii")
    queued_id = upload_test_study(client, headers, "queued.nii")

    db = client.app.state.testing_session_local()
    old_study = db.get(Study, old_id)
    protected_study = db.get(Study, protected_id)
    queued_study = db.get(Study, queued_id)
    old_study.status = StudyStatus.completed
    protected_study.status = StudyStatus.completed
    protected_study.keep_forever = True
    queued_study.status = StudyStatus.queued
    old_study.created_at = datetime.utcnow() - timedelta(days=120)
    protected_study.created_at = datetime.utcnow() - timedelta(days=120)
    queued_study.created_at = datetime.utcnow() - timedelta(days=120)
    old_dir = Path(old_study.output_path).parent
    protected_dir = Path(protected_study.output_path).parent
    queued_dir = Path(queued_study.output_path).parent
    storage = LocalStudyStorage(old_dir.parent)
    db.commit()

    result = apply_retention_policy(db, storage, 90, dry_run=False)

    assert [candidate.id for candidate in result.deleted] == [old_id]
    assert db.get(Study, old_id).deleted_at is not None
    assert db.get(Study, protected_id).deleted_at is None
    assert db.get(Study, queued_id).deleted_at is None
    assert not old_dir.exists()
    assert protected_dir.exists()
    assert queued_dir.exists()
    event = (
        db.query(AuditEvent)
        .filter(AuditEvent.event_type == "study_retention_deleted")
        .one()
    )
    assert event.study_id == old_id
    assert event.actor == "system/retention"
    db.close()


def test_retention_cli_blocks_execute_when_disabled(monkeypatch):
    monkeypatch.setattr("sys.argv", ["retention", "--execute"])

    try:
        retention_cli.main()
    except SystemExit as exc:
        assert "RETENTION_ENABLED=true" in str(exc)
    else:
        raise AssertionError("retention execute should require RETENTION_ENABLED=true")


def test_admin_can_create_researcher_user(client):
    admin_headers, _ = auth_headers(
        client, "admin@example.org", "secret-pass", role=UserRole.admin.value
    )

    response = client.post(
        "/api/users",
        json={
            "email": "new@example.org",
            "full_name": "Nuevo Usuario",
            "password": "new-secret",
            "role": "researcher",
            "storage_quota_bytes": 2048,
        },
        headers=admin_headers,
    )

    assert response.status_code == 201
    assert response.json()["email"] == "new@example.org"
    assert response.json()["role"] == "researcher"
    assert response.json()["storage_quota_bytes"] == 2048


def test_admin_can_update_user_quota_and_delete_user_logically(client):
    admin_headers, _ = auth_headers(
        client, "admin@example.org", "secret-pass", role=UserRole.admin.value
    )
    user_id = create_test_user(client, "researcher@example.org", "secret-pass")

    updated = client.patch(
        f"/api/users/{user_id}",
        json={"storage_quota_bytes": 1024, "is_active": False},
        headers=admin_headers,
    )
    deleted = client.delete(f"/api/users/{user_id}", headers=admin_headers)
    listed = client.get("/api/users", headers=admin_headers)

    assert updated.status_code == 200
    assert updated.json()["storage_quota_bytes"] == 1024
    assert updated.json()["is_active"] is False
    assert deleted.status_code == 200
    assert deleted.json()["message"] == "Usuario borrado"
    assert all(item["id"] != str(user_id) for item in listed.json())

    db = client.app.state.testing_session_local()
    user = db.get(User, user_id)
    event = db.query(AuditEvent).filter(AuditEvent.event_type == "user_deleted").one()
    assert user.deleted_at is not None
    assert user.is_active is False
    assert user.email.startswith("researcher+deleted-")
    assert event.actor == "admin@example.org"
    db.close()


def test_admin_can_create_clean_user_after_logical_delete(client):
    admin_headers, _ = auth_headers(
        client, "admin@example.org", "secret-pass", role=UserRole.admin.value
    )
    old_user_id = create_test_user(client, "researcher@example.org", "secret-pass")

    deleted = client.delete(f"/api/users/{old_user_id}", headers=admin_headers)
    recreated = client.post(
        "/api/users",
        json={
            "email": "researcher@example.org",
            "full_name": "Nuevo Usuario",
            "password": "new-secret",
            "role": "researcher",
        },
        headers=admin_headers,
    )

    assert deleted.status_code == 200
    assert recreated.status_code == 201
    assert recreated.json()["email"] == "researcher@example.org"
    assert recreated.json()["id"] != str(old_user_id)

    db = client.app.state.testing_session_local()
    old_user = db.get(User, old_user_id)
    new_user = db.get(User, UUID(recreated.json()["id"]))
    assert old_user.deleted_at is not None
    assert old_user.email != "researcher@example.org"
    assert new_user.deleted_at is None
    assert new_user.email == "researcher@example.org"
    db.close()


def test_create_user_validation_error_returns_structured_detail(client):
    admin_headers, _ = auth_headers(
        client, "admin@example.org", "secret-pass", role=UserRole.admin.value
    )

    response = client.post(
        "/api/users",
        json={
            "email": "froga@froga.eus",
            "full_name": "Froga",
            "password": "short",
            "role": "researcher",
        },
        headers=admin_headers,
    )

    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)


def test_admin_cannot_delete_own_user(client):
    admin_headers, admin_id = auth_headers(
        client, "admin@example.org", "secret-pass", role=UserRole.admin.value
    )

    response = client.delete(f"/api/users/{admin_id}", headers=admin_headers)

    assert response.status_code == 409


def test_upload_rejects_user_storage_quota_exceeded(client):
    headers, user_id = auth_headers(client, "researcher@example.org", "secret-pass")

    db = client.app.state.testing_session_local()
    user = db.get(User, user_id)
    user.storage_quota_bytes = 5
    db.commit()
    db.close()

    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "La subida supera la cuota de almacenamiento del usuario"
    assert client.get("/api/studies", headers=headers).json() == []


def test_auth_me_includes_user_storage_quota_usage(client):
    headers, user_id = auth_headers(client, "researcher@example.org", "secret-pass")

    db = client.app.state.testing_session_local()
    user = db.get(User, user_id)
    user.storage_quota_bytes = 1024
    db.commit()
    db.close()

    upload = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    current = client.get("/api/auth/me", headers=headers)

    assert upload.status_code == 201
    assert current.status_code == 200
    assert current.json()["storage_quota_bytes"] == 1024
    assert current.json()["storage_used_bytes"] == len(b"dummy image")


def test_admin_and_owner_can_update_study_clinical_review_status(client):
    researcher_headers, _ = auth_headers(
        client, "researcher@example.org", "secret-pass"
    )
    admin_headers, _ = auth_headers(
        client, "admin@example.org", "secret-pass", role=UserRole.admin.value
    )
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=researcher_headers,
    )
    study_id = response.json()["id"]
    complete_study_with_pdf(client, study_id)

    updated = client.patch(
        f"/api/studies/{study_id}/clinical-review",
        json={"clinical_review_status": "reviewed"},
        headers=admin_headers,
    )
    owner_updated = client.patch(
        f"/api/studies/{study_id}/clinical-review",
        json={"clinical_review_status": "validated"},
        headers=researcher_headers,
    )
    invalid = client.patch(
        f"/api/studies/{study_id}/clinical-review",
        json={"clinical_review_status": "clinical_report"},
        headers=admin_headers,
    )

    assert updated.status_code == 200
    assert updated.json()["clinical_review_status"] == "reviewed"
    assert owner_updated.status_code == 200
    assert owner_updated.json()["clinical_review_status"] == "validated"
    assert invalid.status_code == 400


def test_researcher_cannot_update_other_user_clinical_review_status(client):
    owner_headers, _ = auth_headers(client, "owner@example.org", "secret-pass")
    other_headers, _ = auth_headers(client, "other@example.org", "secret-pass")
    response = client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=owner_headers,
    )
    study_id = response.json()["id"]
    complete_study_with_pdf(client, study_id)

    update = client.patch(
        f"/api/studies/{study_id}/clinical-review",
        json={"clinical_review_status": "reviewed"},
        headers=other_headers,
    )

    assert update.status_code == 404


def test_user_can_update_notification_preferences(client):
    headers, user_id = auth_headers(client, "researcher@example.org", "secret-pass")

    current = client.get("/api/me/notification-preferences", headers=headers)
    update = client.patch(
        "/api/me/notification-preferences",
        json={
            "notify_on_processing_completed": False,
            "notify_on_processing_failed": True,
        },
        headers=headers,
    )

    assert current.status_code == 200
    assert current.json()["notify_on_processing_completed"] is True
    assert update.status_code == 200
    assert update.json() == {
        "notify_on_processing_completed": False,
        "notify_on_processing_failed": True,
    }

    db = client.app.state.testing_session_local()
    user = db.get(User, user_id)
    assert user.notify_on_processing_completed is False
    db.close()


def test_user_lists_and_marks_own_notifications(client):
    headers, user_id = auth_headers(client, "researcher@example.org", "secret-pass")
    other_id = create_test_user(client, "other@example.org", "secret-pass")

    db = client.app.state.testing_session_local()
    own = Notification(
        recipient_user_id=user_id,
        event_type="processing_completed",
        title="Listo",
        message="Tu estudio terminó",
        email_status="disabled",
    )
    other = Notification(
        recipient_user_id=other_id,
        event_type="processing_failed",
        title="Fallido",
        message="Otro estudio falló",
        email_status="disabled",
    )
    db.add_all([own, other])
    db.commit()
    own_id = own.id
    other_id = other.id
    db.close()

    listed = client.get("/api/notifications", headers=headers)
    marked = client.post(f"/api/notifications/{own_id}/read", headers=headers)
    forbidden = client.post(f"/api/notifications/{other_id}/read", headers=headers)

    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [str(own_id)]
    assert marked.status_code == 200
    assert marked.json()["read_at"] is not None
    assert forbidden.status_code == 404


def test_researcher_cannot_create_users(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")

    response = client.post(
        "/api/users",
        json={
            "email": "new@example.org",
            "full_name": "Nuevo Usuario",
            "password": "new-secret",
            "role": "researcher",
        },
        headers=headers,
    )

    assert response.status_code == 403


def test_admin_dashboard_returns_operational_summary(client, monkeypatch):
    import app.api.routes as routes

    monkeypatch.setattr(
        routes,
        "_redis_health",
        lambda settings: AdminServiceHealth(name="Redis", status="ok"),
    )
    monkeypatch.setattr(
        routes,
        "_inspect_worker_stats",
        lambda: (
            {
                "celery@worker-1": {"pool": {"max-concurrency": 1}},
                "celery@worker-2": {"pool": {"max-concurrency": 1}},
            },
            None,
        ),
    )
    researcher_headers, _ = auth_headers(
        client, "researcher@example.org", "secret-pass"
    )
    admin_headers, _ = auth_headers(
        client, "admin@example.org", "secret-pass", role=UserRole.admin.value
    )
    queued = client.post(
        "/api/studies/upload",
        files={"file": ("queued.nii", b"dummy image", "application/octet-stream")},
        headers=researcher_headers,
    ).json()
    failed = client.post(
        "/api/studies/upload",
        files={"file": ("failed.nii", b"dummy image", "application/octet-stream")},
        headers=researcher_headers,
    ).json()

    db = client.app.state.testing_session_local()
    failed_study = db.get(Study, UUID(failed["id"]))
    failed_study.status = StudyStatus.failed
    failed_job = (
        db.query(ProcessingJob)
        .filter(ProcessingJob.study_id == UUID(failed["id"]))
        .one()
    )
    failed_job.status = StudyStatus.failed.value
    db.commit()
    db.close()

    response = client.get("/api/admin/dashboard", headers=admin_headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["queue"]["queued"] == 1
    assert payload["queue"]["failed"] == 1
    assert payload["queue"]["active"] == 1
    assert payload["queue"]["worker_replicas"] == 2
    assert payload["queue"]["worker_concurrency"] == 1
    assert payload["queue"]["processing_capacity"] == 2
    assert payload["queue"]["processing_available"] == 2
    assert payload["studies_by_status"]["queued"] == 1
    assert payload["studies_by_status"]["failed"] == 1
    assert payload["users"]["admins"] == 1
    assert payload["users"]["researchers"] == 1
    assert payload["storage"]["exists"] is True
    assert payload["storage"]["studies_bytes"] > 0
    assert payload["recent_failed_jobs"][0]["study_id"] == failed["id"]
    assert "Hay 1 job(s) fallidos que requieren revisión" in payload["alerts"]
    assert UUID(queued["id"])


def test_researcher_cannot_access_admin_dashboard(client):
    headers, _ = auth_headers(client, "researcher@example.org", "secret-pass")

    response = client.get("/api/admin/dashboard", headers=headers)

    assert response.status_code == 403


def test_compneuro_upload_rejects_non_nifti_gz(compneuro_client):
    headers, _ = auth_headers(compneuro_client, "researcher@example.org", "secret-pass")
    response = compneuro_client.post(
        "/api/studies/upload",
        files={"file": ("study.nii", b"dummy image", "application/octet-stream")},
        headers=headers,
    )

    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "Para compneuro-anatproc se espera un fichero .nii.gz"
    )


def test_compneuro_upload_generates_bids_subject(compneuro_client):
    headers, _ = auth_headers(compneuro_client, "researcher@example.org", "secret-pass")
    response = compneuro_client.post(
        "/api/studies/upload",
        files={"file": ("study.nii.gz", b"dummy image", "application/octet-stream")},
        headers=headers,
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["bids_subject_id"].startswith("sub-")

    studies = compneuro_client.get("/api/studies", headers=headers).json()
    assert studies[0]["processor_backend"] == "compneuro"
    assert studies[0]["bids_subject_id"] == payload["bids_subject_id"]


def test_compneuro_upload_cleans_storage_when_bids_preparation_fails(
    compneuro_client, monkeypatch
):
    headers, _ = auth_headers(compneuro_client, "researcher@example.org", "secret-pass")

    def fail_bids_preparation(*args, **kwargs):
        raise RuntimeError("forced failure")

    import app.api.routes as routes

    monkeypatch.setattr(
        routes, "prepare_single_subject_t1w_bids", fail_bids_preparation
    )

    response = compneuro_client.post(
        "/api/studies/upload",
        files={"file": ("study.nii.gz", b"dummy image", "application/octet-stream")},
        headers=headers,
    )

    assert response.status_code == 500
    assert compneuro_client.get("/api/studies", headers=headers).json() == []


def create_test_user(
    client, email: str, password: str, role: str = UserRole.researcher.value
):
    db = client.app.state.testing_session_local()
    user = User(
        email=normalize_email(email),
        full_name="Test User",
        hashed_password=hash_password(password),
        role=role,
        is_active=True,
    )
    db.add(user)
    db.commit()
    user_id = user.id
    db.close()
    return user_id


def auth_headers(
    client, email: str, password: str, role: str = UserRole.researcher.value
):
    user_id = create_test_user(client, email, password, role=role)
    response = client.post(
        "/api/auth/login", json={"email": email, "password": password}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}, user_id


def upload_test_study(client, headers, filename: str = "study.nii") -> UUID:
    response = client.post(
        "/api/studies/upload",
        files={"file": (filename, b"dummy image", "application/octet-stream")},
        headers=headers,
    )
    assert response.status_code == 201
    return UUID(response.json()["id"])


def complete_study_with_pdf(client, study_id: str) -> Path:
    db = client.app.state.testing_session_local()
    study = db.get(Study, UUID(study_id))
    report_path = Path(study.output_path) / "reports" / "technical_report.pdf"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    study.status = StudyStatus.completed
    study.pdf_path = str(report_path)
    db.commit()
    db.close()
    return report_path
