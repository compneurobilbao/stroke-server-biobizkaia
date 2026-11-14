# API

Swagger is available at `/api/docs`.

## Endpoints

- `GET /api/health`: healthcheck.
- `POST /api/auth/login`: local login with email and password.
- `POST /api/auth/logout`: record a client-side logout.
- `GET /api/auth/me`: current authenticated user.
- `GET /api/me/notification-preferences`: email preferences of the authenticated user.
- `PATCH /api/me/notification-preferences`: update the email preferences of the authenticated user.
- `GET /api/notifications`: list the internal notifications of the authenticated user.
- `POST /api/notifications/{notification_id}/read`: mark one of the user's own notifications as read.
- `GET /api/admin/dashboard`: global operational summary. Admin only.
- `GET /api/users`: list users. Admin only.
- `POST /api/users`: create a user. Admin only.
- `PATCH /api/users/{user_id}`: update active state or storage quota. Admin only.
- `DELETE /api/users/{user_id}`: soft-delete a user. Admin only.
- `POST /api/studies/upload`: upload a file and enqueue processing. Requires authentication.
- `GET /api/studies`: list visible studies. Admin sees all; researcher sees only their own.
- `GET /api/studies/{study_id}`: details if the user has permission.
- `GET /api/studies/{study_id}/detail`: extended details with associated jobs.
- `GET /api/studies/{study_id}/status`: status if the user has permission.
- `PATCH /api/studies/{study_id}/clinical-review`: mark the technical result as `technical_only`, `reviewed` or `validated`.
- `GET /api/studies/{study_id}/logs`: truncated technical logs if the user has permission.
- `POST /api/studies/{study_id}/cancel`: cancel a queued job or request termination of a running one.
- `POST /api/studies/{study_id}/retry`: retry a failed study.
- `DELETE /api/studies/{study_id}`: soft delete in the DB and physical deletion of files if the study is not processing.
- `GET /api/studies/{study_id}/download`: download the technical PDF if completed. Compatible alias.
- `GET /api/studies/{study_id}/download/pdf`: download the technical PDF if the user has permission.
- `GET /api/studies/{study_id}/download/zip`: download the results ZIP if it exists and the user has permission.
- `POST /api/studies/{study_id}/share-links`: create a temporary link to download the technical PDF. Requires owner or admin.
- `GET /api/studies/{study_id}/share-links`: list the share links of a study. Requires owner or admin.
- `POST /api/studies/{study_id}/share-links/{link_id}/revoke`: revoke a share link. Requires owner or admin.
- `GET /api/share/{token}/pdf`: download the technical PDF with a temporary token, without login.

Except for `GET /api/health` and `POST /api/auth/login`, functional endpoints require `Authorization: Bearer <token>`.

The additional functional exception is `GET /api/share/{token}/pdf`: it needs no JWT because the opaque token itself enables a temporary PDF download.

## Authentication

`POST /api/auth/login` uses JSON:

```json
{
  "email": "user@example.org",
  "password": "password"
}
```

Response:

```json
{
  "access_token": "...",
  "token_type": "bearer",
  "user": {
    "id": "...",
    "email": "user@example.org",
    "full_name": "User",
    "role": "researcher",
    "is_active": true
  }
}
```

Initial roles:

- `admin`: sees all studies, manages users/quotas, accesses the operational dashboard and can mark technical review.
- `researcher`: uploads studies, checks their quota, lists and downloads only their own studies and can mark technical review of their own studies.

## Admin Dashboard

`GET /api/admin/dashboard` returns a global operational snapshot for administration:

- queue: queued, processing, failed and active jobs.
- users: totals, active, admins and researchers.
- studies and jobs grouped by status.
- storage: configured path, existence, registered bytes and free/used disk.
- services: PostgreSQL, Redis and Worker with status `ok`, `warning`, `down` or `unknown`.
- latest failed jobs.
- basic non-blocking alerts.

This endpoint exposes no clinical data and runs no processing; it aggregates state already recorded in the platform.

## Upload

`POST /api/studies/upload` uses `multipart/form-data`:

- `file`: study file.
- `bids_subject_id`: optional, format `sub-XXXX`, for example `sub-O01`.

With `PROCESSOR_BACKEND=compneuro`, the file must be `.nii.gz` and the API automatically prepares `bids_project/data`.

Each study is associated with the authenticated user through `owner_user_id`.

If the user has `storage_quota_bytes` defined, the API rejects the upload when the size of the new file exceeds their available quota.

## Job Management

`GET /api/studies/{study_id}/detail` returns the study metadata and the list of associated `ProcessingJob`s.

`GET /api/studies/{study_id}/logs?lines=200` returns, if they exist, `processor.log` and `rendering.log` truncated to the last requested lines. The maximum allowed is 1000 lines.

`POST /api/studies/{study_id}/cancel` works if the study is in `queued` or `processing` state. When queued it revokes the pending task and marks the study as `canceled`; during processing it records the request, revokes the Celery task with `SIGTERM` and the worker/adapter try to propagate the cancellation to the external process. The immediate response may still show `processing` until the worker closes the task.

`POST /api/studies/{study_id}/retry` only works if the study is in `failed` state. It creates a new `ProcessingJob` and re-enqueues the processing.

`DELETE /api/studies/{study_id}` applies a soft delete with `deleted_at`, records an audit event and physically deletes the study folder. If the study is `processing`, it responds `409`.

## Report Sharing

`POST /api/studies/{study_id}/share-links` creates a temporary link for a completed study with an available PDF. The body is optional:

```json
{
  "expires_in_hours": 72
}
```

The response includes the full link only at creation time:

```json
{
  "id": "...",
  "study_id": "...",
  "url": "http://localhost/api/share/<token>/pdf",
  "created_at": "...",
  "expires_at": "...",
  "revoked_at": null,
  "last_accessed_at": null,
  "access_count": 0,
  "is_expired": false,
  "is_revoked": false
}
```

The token is not stored in clear text: the database keeps only a hash. `GET /api/studies/{study_id}/share-links` lists link metadata but cannot recover tokens already created. If a link is lost, it must be revoked or a new one created.

`GET /api/share/{token}/pdf` downloads only the technical PDF if the token exists, has not expired and has not been revoked. Invalid, expired or revoked tokens respond `404` to avoid facilitating enumeration.

Every creation, revocation and token download is recorded in the audit log.

## Expected Errors

The list below describes the status codes and the situations that trigger them. Error message texts are currently emitted in Spanish by the API; they are being translated in the code and are not documented here.

- `400`: extension not allowed.
- `400`: invalid BIDS subject.
- `401`: unauthenticated user or invalid token.
- `403`: authenticated user without sufficient permissions.
- `404`: study or PDF not found.
- `404`: ZIP not available.
- `404`: invalid, expired or revoked public link.
- `409`: operation not allowed for the current job state.
- `409`: attempt to share a study without a completed PDF.
- `413`: file too large.

## Allowed Extensions

Upload accepts the extensions configured in `ALLOWED_EXTENSIONS`:

- `.nii`
- `.nii.gz`
- `.dcm`
- `.zip`
- `.tar`
- `.tar.gz`
- `.gz`
- `.json`
- `.txt`

For `compneuro`, set `ALLOWED_EXTENSIONS=.nii.gz`.

## Versioned API (`/api/v1`)

The case and run API of the pipeline platform. It is served by the same application next to the legacy `/api/studies` endpoints, which are unchanged. Its OpenAPI document is published at `GET /api/v1/openapi.json` (Swagger UI at `/api/v1/docs`) and as a committed snapshot, [`docs/api/openapi-v1.json`](api/openapi-v1.json). A test fails when the snapshot is stale; regenerate it with `PYTHONPATH=.:backend python -m app.api.v1.openapi docs/api/openapi-v1.json`.

All endpoints need `Authorization: Bearer <token>` (same login as the legacy API). Errors have the body `{"detail": "...", "code": "..."}`, plus `"slot"` when a slot is meant; clients key on `code`.

### Endpoints

| Endpoint | Purpose |
|---|---|
| `GET /pipelines` | Loaded pipelines (id, version, inputs, input sets). |
| `GET /pipelines/{id}/versions/{version}` | One pipeline with its slots. |
| `GET /pipelines/{id}/versions/{version}/slots` | Slots with their **enabled** implementations, the deployment default and the pipeline default (`include_disabled=true`: administrators). |
| `POST /cases` | Create a case (`sex`, `age_band` only). |
| `GET /cases`, `GET /cases/{id}` | List and read cases (with their input list). |
| `PATCH /cases/{id}` | Edit `sex` and `age_band` (`null` clears); `keep_forever` for administrators. Existing runs keep the values frozen at their creation. |
| `DELETE /cases/{id}` | Soft delete and remove files; refused (409) while a run is active or when `keep_forever` is set. |
| `PUT /cases/{id}/inputs/{role}` | Upload (201) or replace (200) the input of a role, multipart field `file`. Roles come from the loaded pipelines, for example `ct`, `lesion_native`, `lesion_mni`. Allowed until the first run is created. |
| `GET /cases/{id}/inputs`, `GET .../inputs/{role}/download`, `DELETE .../inputs/{role}` | List, download, remove an input. |
| `POST /cases/{id}/runs` | Start a run: `pipeline_id`, optional `pipeline_version` (default: highest), `implementations` (slot to implementation id), `input_roles` (subset), `seed`. Returns the queued run (201) and enqueues `worker.tasks.run_pipeline`. |
| `GET /cases/{id}/runs` | Runs of a case, newest first. |
| `GET /runs/{id}` | Status, slot choices (implementation, digest, parameters, bundles) and the step timeline (one entry per attempt). |
| `GET /runs/{id}/steps` | The step timeline alone. |
| `GET /runs/{id}/steps/{step_run_id}/log` | Last `tail_bytes` of an attempt log (`X-Log-Truncated`). |
| `GET /runs/{id}/provenance` | The run provenance record. |
| `GET /runs/{id}/artifacts`, `GET .../artifacts/{artifact_id}/download` | Artefacts of the run. |
| `GET /runs/{id}/reports`, `GET .../reports/{report_id}/pdf` | Reports of the run. |
| `POST /runs/{id}/cancel` | 200 when canceled, 202 while a running run is asked to stop; idempotent; 409 `run_not_active` for a finished run. |
| `POST /runs/{id}/resume` (alias `/retry`) | Resume a failed or canceled run from the failed step: 202 when queued, 200 when already active. |
| `GET /admin/implementations` | Administrators: every allowed implementation with its setting (no setting means disabled). |
| `PUT /admin/implementations/{pipeline_id}/{slot}/{implementation_id}` | Administrators: `{"enabled": bool, "is_default": bool}`; a default must be enabled and replaces the slot's previous default. |

### Permissions

Matrix: [data-model.md section 9](data-model.md). Phase 0 enforces `admin` and `researcher`. Order of checks: 401 without a valid token; 403 when the role has no right to the action; 404 for a case or run the user cannot see (not theirs, deleted, or a legacy case), so that existence is not revealed. A researcher acts on their own cases; an administrator on all. `reviewer` and `coordinator` act on granted cases only and no grants exist before F1-03, so they see no case; `service` is refused everywhere. Only administrators set `keep_forever` and manage implementations.

### Quota

The storage quota of the case owner counts legacy uploads, input files and stored artefacts. An upload that would exceed it is refused with 413 `storage_quota_exceeded`, as is starting a run when the owner is already at or over the quota.

### Run creation errors

| Status | Codes |
|---|---|
| 404 | `pipeline_not_found`, `case_not_found`, `run_not_found`, `input_not_found`, `artifact_not_found`, `report_not_found`, `step_run_not_found`, `log_not_available`, `provenance_not_available`, `file_missing_in_storage` |
| 422 | `unknown_slot`, `implementation_not_enabled` (a user choice that is not allowed or not enabled; body names the slot), `no_matching_input_set`, `input_type_mismatch`, `unknown_input_role`, `ambiguous_input_role`, `file_extension_not_allowed`, `invalid_input_file` |
| 409 | `no_implementation` (a required slot has no enabled implementation; names the slot), `no_inputs`, `active_run_exists`, `case_has_runs`, `case_has_active_run`, `case_keep_forever`; resume and cancel: `run_not_active`, `run_succeeded`, `cancel_in_progress`, `input_rejected`, `pipeline_unavailable`, `implementation_disabled`, `bundle_unavailable`; resolution: `slot_unsatisfiable`, `manifest_unavailable`, `implementation_invalid`, `invalid_parameters`, `bundle_unavailable` |
| 413 | `upload_too_large`, `storage_quota_exceeded` |

### Configuration

| Variable | Default | Meaning |
|---|---|---|
| `PIPELINES_DIR`, `STEP_MANIFESTS_DIR`, `BUNDLE_ROOT` | as in [processing-pipeline.md](processing-pipeline.md) | Read by the API process too (loaded once at start). |
| `PLATFORM_VERSION` | `0.0.0-dev` | Recorded on new runs. |
| `DEPLOYMENT_PROFILE` | `research-public` | Profile stored on new cases (`research-public`, `mask-only`, `on-premise`). |

### Privacy

A case holds a platform-generated label, an optional sex and an age band; no exact age, name or free-text identifier is accepted (unknown fields are rejected). The name of an uploaded file is only used to check its extension and is stored nowhere, including the audit log.
