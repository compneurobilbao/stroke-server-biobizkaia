from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class StudyRead(BaseModel):
    id: UUID
    owner_user_id: UUID
    original_filename: str
    status: str
    created_at: datetime
    updated_at: datetime
    processing_started_at: datetime | None = None
    processing_finished_at: datetime | None = None
    deleted_at: datetime | None = None
    keep_forever: bool = False
    error_message: str | None = None
    processor_name: str | None = None
    processor_version: str | None = None
    processor_backend: str | None = None
    bids_subject_id: str | None = None
    file_size: int | None = None
    checksum: str | None = None
    has_pdf: bool = False
    has_output_zip: bool = False
    processing_warnings: str | None = None
    clinical_review_status: str = "technical_only"

    model_config = {"from_attributes": True}


class StudyStatusRead(BaseModel):
    id: UUID
    status: str
    error_message: str | None = None
    has_pdf: bool
    has_output_zip: bool = False
    processing_warnings: str | None = None
    clinical_review_status: str = "technical_only"
    updated_at: datetime


class ProcessingJobRead(BaseModel):
    id: UUID
    study_id: UUID
    celery_task_id: str | None = None
    status: str
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    retry_count: int
    worker_name: str | None = None
    exit_code: int | None = None
    log_path: str | None = None
    error_message: str | None = None

    model_config = {"from_attributes": True}


class StudyDetailRead(StudyRead):
    jobs: list[ProcessingJobRead] = Field(default_factory=list)


class StudyLogEntry(BaseModel):
    name: str
    content: str
    truncated: bool


class StudyLogsRead(BaseModel):
    study_id: UUID
    logs: list[StudyLogEntry] = Field(default_factory=list)


class ShareLinkCreate(BaseModel):
    expires_in_hours: int | None = Field(default=None, ge=1, le=720)


class ShareLinkRead(BaseModel):
    id: UUID
    study_id: UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    last_accessed_at: datetime | None = None
    access_count: int
    is_expired: bool
    is_revoked: bool


class ShareLinkCreateResponse(ShareLinkRead):
    url: str


class StudyActionResponse(BaseModel):
    id: UUID
    status: str
    message: str


class ClinicalReviewUpdate(BaseModel):
    clinical_review_status: str


class UploadResponse(BaseModel):
    id: UUID
    status: str
    message: str
    bids_subject_id: str | None = None
