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
