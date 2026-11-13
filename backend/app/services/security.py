from __future__ import annotations

import re
from pathlib import Path
from uuid import UUID

from fastapi import UploadFile

from app.core.errors import ErrorCode, api_error


def sanitize_filename(filename: str) -> str:
    name = Path(filename).name.strip().replace(" ", "_")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name or name in {".", ".."}:
        raise api_error(ErrorCode.INVALID_FILENAME)
    return name[:255]


def has_allowed_extension(filename: str, allowed_extensions: list[str]) -> bool:
    lower_name = filename.lower()
    return any(lower_name.endswith(extension) for extension in allowed_extensions)


def validate_upload(upload: UploadFile, allowed_extensions: list[str]) -> str:
    safe_name = sanitize_filename(upload.filename or "")
    if not has_allowed_extension(safe_name, allowed_extensions):
        raise api_error(ErrorCode.FILE_EXTENSION_NOT_ALLOWED)
    return safe_name


BIDS_SUBJECT_PATTERN = re.compile(r"^sub-[A-Za-z0-9]+$")


def validate_nifti_gz_filename(filename: str) -> None:
    if not filename.lower().endswith(".nii.gz"):
        raise api_error(ErrorCode.NIFTI_GZ_REQUIRED)


def resolve_bids_subject_id(raw_subject_id: str | None, study_id: UUID) -> str:
    subject_id = (raw_subject_id or "").strip()
    if not subject_id:
        return f"sub-{study_id.hex[:8]}"
    if not BIDS_SUBJECT_PATTERN.fullmatch(subject_id):
        raise api_error(ErrorCode.INVALID_BIDS_SUBJECT_ID)
    return subject_id
