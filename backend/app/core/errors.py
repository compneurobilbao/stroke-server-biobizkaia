"""Stable API error codes.

Every error raised on purpose by the API carries a machine-readable ``code``
next to the human-readable ``detail``. The response body looks like::

    {"detail": "Study not found", "code": "study_not_found"}

``detail`` is English text meant for people and may be reworded; clients must
key on ``code``. Use ``api_error(ErrorCode.X)`` to build the exception; the
HTTP status and default message of each code are defined once in ``_CATALOG``.

Codes (HTTP status):

Authentication and authorisation
    invalid_credentials (401), not_authenticated (401), permission_denied (403)
Users
    invalid_role (400), user_email_exists (409), user_not_found (404),
    cannot_deactivate_self (409), cannot_delete_self (409),
    last_admin_required (409)
Notifications
    notification_not_found (404)
Uploads
    invalid_filename (400), file_extension_not_allowed (400),
    nifti_gz_required (400), invalid_bids_subject_id (400),
    upload_too_large (413), storage_quota_exceeded (413),
    bids_preparation_failed (500)
Studies
    study_not_found (404), invalid_clinical_review_status (400),
    clinical_review_requires_completed (409), pdf_not_ready (404),
    pdf_missing_in_storage (404), zip_not_ready (404),
    zip_missing_in_storage (404), cancel_invalid_state (409),
    cancel_without_task (409), processing_not_active (409),
    retry_invalid_state (409), delete_while_processing (409)
Share links
    share_requires_completed_pdf (409), share_link_creation_failed (500),
    share_link_not_found (404), report_not_found (404)
"""

from __future__ import annotations

from enum import Enum

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse


class ErrorCode(str, Enum):
    INVALID_CREDENTIALS = "invalid_credentials"
    NOT_AUTHENTICATED = "not_authenticated"
    PERMISSION_DENIED = "permission_denied"
    INVALID_ROLE = "invalid_role"
    USER_EMAIL_EXISTS = "user_email_exists"
    USER_NOT_FOUND = "user_not_found"
    CANNOT_DEACTIVATE_SELF = "cannot_deactivate_self"
    CANNOT_DELETE_SELF = "cannot_delete_self"
    LAST_ADMIN_REQUIRED = "last_admin_required"
    NOTIFICATION_NOT_FOUND = "notification_not_found"
    INVALID_FILENAME = "invalid_filename"
    FILE_EXTENSION_NOT_ALLOWED = "file_extension_not_allowed"
    NIFTI_GZ_REQUIRED = "nifti_gz_required"
    INVALID_BIDS_SUBJECT_ID = "invalid_bids_subject_id"
    UPLOAD_TOO_LARGE = "upload_too_large"
    STORAGE_QUOTA_EXCEEDED = "storage_quota_exceeded"
    BIDS_PREPARATION_FAILED = "bids_preparation_failed"
    STUDY_NOT_FOUND = "study_not_found"
    INVALID_CLINICAL_REVIEW_STATUS = "invalid_clinical_review_status"
    CLINICAL_REVIEW_REQUIRES_COMPLETED = "clinical_review_requires_completed"
    PDF_NOT_READY = "pdf_not_ready"
    PDF_MISSING_IN_STORAGE = "pdf_missing_in_storage"
    ZIP_NOT_READY = "zip_not_ready"
    ZIP_MISSING_IN_STORAGE = "zip_missing_in_storage"
    CANCEL_INVALID_STATE = "cancel_invalid_state"
    CANCEL_WITHOUT_TASK = "cancel_without_task"
    PROCESSING_NOT_ACTIVE = "processing_not_active"
    RETRY_INVALID_STATE = "retry_invalid_state"
    DELETE_WHILE_PROCESSING = "delete_while_processing"
    SHARE_REQUIRES_COMPLETED_PDF = "share_requires_completed_pdf"
    SHARE_LINK_CREATION_FAILED = "share_link_creation_failed"
    SHARE_LINK_NOT_FOUND = "share_link_not_found"
    REPORT_NOT_FOUND = "report_not_found"


# code -> (HTTP status, default English message)
_CATALOG: dict[ErrorCode, tuple[int, str]] = {
    ErrorCode.INVALID_CREDENTIALS: (401, "Invalid credentials"),
    ErrorCode.NOT_AUTHENTICATED: (401, "Not authenticated"),
    ErrorCode.PERMISSION_DENIED: (
        403,
        "You do not have permission to perform this operation",
    ),
    ErrorCode.INVALID_ROLE: (400, "Invalid role"),
    ErrorCode.USER_EMAIL_EXISTS: (409, "A user with this email address already exists"),
    ErrorCode.USER_NOT_FOUND: (404, "User not found"),
    ErrorCode.CANNOT_DEACTIVATE_SELF: (
        409,
        "You cannot deactivate your own administrator account",
    ),
    ErrorCode.CANNOT_DELETE_SELF: (
        409,
        "You cannot delete your own administrator account",
    ),
    ErrorCode.LAST_ADMIN_REQUIRED: (
        409,
        "At least one active administrator must remain",
    ),
    ErrorCode.NOTIFICATION_NOT_FOUND: (404, "Notification not found"),
    ErrorCode.INVALID_FILENAME: (400, "Invalid file name"),
    ErrorCode.FILE_EXTENSION_NOT_ALLOWED: (400, "File extension not allowed"),
    ErrorCode.NIFTI_GZ_REQUIRED: (400, "compneuro-anatproc expects a .nii.gz file"),
    ErrorCode.INVALID_BIDS_SUBJECT_ID: (
        400,
        "The BIDS identifier must have the format sub-XXXX, for example sub-O01",
    ),
    ErrorCode.UPLOAD_TOO_LARGE: (413, "The file exceeds the maximum allowed size"),
    ErrorCode.STORAGE_QUOTA_EXCEEDED: (
        413,
        "The upload exceeds the storage quota of the user",
    ),
    ErrorCode.BIDS_PREPARATION_FAILED: (500, "Could not prepare the BIDS structure"),
    ErrorCode.STUDY_NOT_FOUND: (404, "Study not found"),
    ErrorCode.INVALID_CLINICAL_REVIEW_STATUS: (400, "Invalid clinical review status"),
    ErrorCode.CLINICAL_REVIEW_REQUIRES_COMPLETED: (
        409,
        "Only completed studies can be reviewed or validated",
    ),
    ErrorCode.PDF_NOT_READY: (404, "The PDF report is not available yet"),
    ErrorCode.PDF_MISSING_IN_STORAGE: (404, "The PDF report does not exist in storage"),
    ErrorCode.ZIP_NOT_READY: (404, "The results ZIP is not available yet"),
    ErrorCode.ZIP_MISSING_IN_STORAGE: (
        404,
        "The results ZIP does not exist in storage",
    ),
    ErrorCode.CANCEL_INVALID_STATE: (
        409,
        "Only queued or processing jobs can be canceled",
    ),
    ErrorCode.CANCEL_WITHOUT_TASK: (
        409,
        "Cannot cancel a processing job without an associated Celery task",
    ),
    ErrorCode.PROCESSING_NOT_ACTIVE: (409, "Processing is no longer active"),
    ErrorCode.RETRY_INVALID_STATE: (409, "Only failed jobs can be retried"),
    ErrorCode.DELETE_WHILE_PROCESSING: (
        409,
        "A study that is being processed cannot be deleted",
    ),
    ErrorCode.SHARE_REQUIRES_COMPLETED_PDF: (
        409,
        "Only completed studies with an available PDF can be shared",
    ),
    ErrorCode.SHARE_LINK_CREATION_FAILED: (500, "Could not create the share link"),
    ErrorCode.SHARE_LINK_NOT_FOUND: (404, "Share link not found"),
    ErrorCode.REPORT_NOT_FOUND: (404, "Report not found"),
}


class ApiError(HTTPException):
    """HTTPException that also carries a stable machine-readable ``code``."""

    def __init__(self, code: ErrorCode, detail: str | None = None) -> None:
        status_code, default_detail = _CATALOG[code]
        super().__init__(status_code=status_code, detail=detail or default_detail)
        self.code = code.value


def api_error(code: ErrorCode, detail: str | None = None) -> ApiError:
    return ApiError(code, detail)


async def api_error_handler(_: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
        headers=exc.headers,
    )
