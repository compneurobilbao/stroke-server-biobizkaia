# Backend

This folder contains the system's HTTP API. It is implemented with FastAPI and is responsible for authenticating users, receiving studies, validating them, recording metadata in PostgreSQL, enqueueing Celery tasks and serving downloads according to permissions.

## Responsibility

- Expose REST endpoints under `/api`.
- Publish OpenAPI documentation at `/api/docs`.
- Authenticate users with local login and JWT.
- Apply `admin`/`researcher` roles and per-study ownership.
- Validate file extensions, sizes and names.
- Store inputs in `data/studies/{study_id}/input`.
- Create `Study`, `ProcessingJob` and `AuditEvent` records.
- Enqueue asynchronous processing.
- Serve PDF/ZIP downloads once the worker has produced them.
- Create users from the admin panel, edit quotas, soft-delete users and allow the initial admin to be created via CLI.
- Expose job details, truncated logs, cancellation of queued or running jobs, retry of failed jobs and safe deletion.
- Expose an admin dashboard with aggregated metrics, healthchecks and basic alerts.
- Manage temporary PDF download links, preferences/notifications and technical review of results.

## Code Layout

- `app/main.py`: creates the FastAPI application, configures CORS and registers the routes.
- `app/api/routes.py`: defines the HTTP endpoints. It is the API entry layer and does not run heavy processing.
- `app/schemas/admin.py`: Pydantic schemas for the admin dashboard.
- `app/cli/create_admin.py`: command to create or update the initial admin.
- `app/core/config.py`: centralises environment-variable configuration using `pydantic-settings`.
- `app/db/base.py`: defines the SQLAlchemy declarative base class.
- `app/db/session.py`: creates the engine, the session and the `get_db` dependency.
- `app/models/`: contains the persistent tables.
- `app/schemas/`: contains Pydantic models for HTTP responses.
- `app/services/`: contains helper logic for storage, auditing, upload safety and authentication.
- `alembic/`: contains database migrations.
- `requirements.txt`: Python dependencies shared by the API and the worker.
- `Dockerfile`: image used by `api` and `worker`.

## Main Models

- `User`: local users with the `admin` or `researcher` role.
- `Study`: the uploaded study, its paths, status and technical metadata.
- `ProcessingJob`: one concrete run of the asynchronous processing.
- `AuditEvent`: records relevant events for traceability.
- `ShareLink`: temporary, revocable links to download technical PDFs only.
- `Notification`: records internal notices and email delivery status.

## Upload Flow

1. The user obtains a token with `POST /api/auth/login`.
2. `POST /api/studies/upload` receives an authenticated `UploadFile`.
3. `validate_upload` sanitises and validates the name.
4. `LocalStudyStorage.save_upload` stores the file and computes size/checksum.
5. A `Study` with `owner_user_id` and a `ProcessingJob` with status `queued` are created.
6. An audit event is recorded with `record_event` and the acting user.
7. `process_study.delay(...)` is called to delegate the work to the worker.

## Job Management

- `GET /api/studies/{study_id}/detail`: the study and its jobs.
- `GET /api/studies/{study_id}/logs`: truncated `processor.log` and `rendering.log`.
- `POST /api/studies/{study_id}/cancel`: cancels queued jobs or requests termination of running jobs.
- `POST /api/studies/{study_id}/retry`: re-enqueues failed studies.
- `DELETE /api/studies/{study_id}`: soft delete plus physical deletion if it is not processing.

## Admin Dashboard

- `GET /api/admin/dashboard`: global summary, `admin` only.
- Aggregates queue, jobs by status, users, studies by status, storage, healthchecks and alerts.
- It does not invoke processing or access the external processor; it only queries the DB, Redis, the worker and the filesystem.

## Architectural Limits

The API must not import or know the internal details of the external processor. Actual execution is delegated to the worker and always goes through `processor_adapter`.
