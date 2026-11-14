"""Cases and their input files.

Privacy: a case holds only a platform-generated label, an optional sex and an age band.
The name of an uploaded file is used to check its extension and is never stored.
Inputs can be added, replaced or removed until the first run of the case is created.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import os
import shutil
import tempfile
import uuid
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    Orchestration,
    audit_event,
    authorize,
    case_file,
    deployment_profile,
    get_orchestration,
    load_case,
    lock_case,
    lock_user,
    storage_for,
    visible_cases,
)
from app.api.v1.errors import v1_error
from app.api.v1.quota import check_quota
from app.core.config import Settings, get_settings
from app.db.session import get_db
from app.models._common import utcnow
from app.models.case import Case, InputFile
from app.models.pipeline_run import ACTIVE_STATUSES, PipelineRun
from app.models.user import User
from app.schemas.api_v1 import CaseDetailRead, CaseUpdateV1
from app.schemas.case import CaseCreate, CaseRead, InputFileRead
from app.services.auth import client_ip, get_current_user
from app.services.bids import (
    BidsNaming,
    CaseIdentity,
    RetentionBlocked,
    delete_case_files,
    ensure_raw_dataset,
    store_raw_input,
)
from orchestrator.pipeline import artefact_types

log = logging.getLogger(__name__)
router = APIRouter(tags=["cases"])

_CHUNK = 1 << 20
_NIFTI1_HEADER = 348
_NIFTI2_HEADER = 540


def _detail(db: Session, case: Case) -> CaseDetailRead:
    inputs = db.scalars(
        select(InputFile).where(InputFile.case_id == case.id).order_by(InputFile.role)
    ).all()
    return CaseDetailRead.model_validate(
        {
            **CaseRead.model_validate(case).model_dump(),
            "inputs": [InputFileRead.model_validate(i) for i in inputs],
        }
    )


def _owner(db: Session, case: Case) -> User:
    return db.get(User, case.owner_user_id)


def _has_runs(db: Session, case_id: UUID) -> bool:
    return (
        db.scalar(select(PipelineRun.id).where(PipelineRun.case_id == case_id).limit(1))
        is not None
    )


def input_role_types(orch: Orchestration) -> dict[str, set[str]]:
    """Input role -> artefact types declared for it by the loaded pipelines."""
    out: dict[str, set[str]] = {}
    for pipeline in orch.pipelines.all():
        for name, atype in pipeline.inputs.items():
            out.setdefault(name, set()).add(atype)
    return out


@router.post("/cases", response_model=CaseDetailRead, status_code=201)
def create_case(
    body: CaseCreate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CaseDetailRead:
    authorize(current_user, "case.create")
    profile = deployment_profile()
    for _ in range(
        3
    ):  # the label is derived from the id; retry on a (very rare) collision
        case = Case(
            id=uuid.uuid4(),
            owner_user_id=current_user.id,
            deployment_profile=profile,
            sex=body.sex,
            age_band=body.age_band,
        )
        db.add(case)
        try:
            db.flush()
            audit_event(
                db,
                "case_created",
                current_user,
                case_id=case.id,
                ip_address=client_ip(request),
            )
            db.commit()
            break
        except IntegrityError:
            # Only a subject-label collision is expected here; log anything else so a
            # real constraint failure is not hidden behind the generic 500.
            log.warning("Case creation hit an integrity error", exc_info=True)
            db.rollback()
    else:
        raise v1_error(500, "case_creation_failed", "Could not create the case")
    return _detail(db, case)


@router.get("/cases", response_model=list[CaseRead])
def list_cases(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[Case]:
    query = (
        visible_cases(db, current_user, "case.read")
        .order_by(Case.created_at.desc(), Case.id)
        .limit(limit)
        .offset(offset)
    )
    return list(db.scalars(query))


@router.get("/cases/{case_id}", response_model=CaseDetailRead)
def get_case(
    case_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CaseDetailRead:
    return _detail(db, load_case(db, case_id, current_user, "case.read"))


@router.patch("/cases/{case_id}", response_model=CaseDetailRead)
def update_case(
    case_id: UUID,
    body: CaseUpdateV1,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CaseDetailRead:
    """Edit ``sex`` and ``age_band`` (null clears); ``keep_forever`` is for administrators.
    Existing runs keep the values frozen at their creation."""
    case = load_case(db, case_id, current_user, "case.edit")
    changed = body.model_fields_set
    if "keep_forever" in changed:
        authorize(current_user, "case.keep_forever")
        if body.keep_forever is None:
            raise v1_error(422, "invalid_request", "keep_forever must be true or false")
        case.keep_forever = body.keep_forever
    if "sex" in changed:
        case.sex = body.sex
    if "age_band" in changed:
        case.age_band = body.age_band
    case.updated_at = utcnow()
    audit_event(
        db,
        "case_updated",
        current_user,
        case_id=case.id,
        details={"fields": sorted(changed)},
        ip_address=client_ip(request),
    )
    db.commit()
    storage = storage_for(settings)
    if storage.rawdata_dir(case.id).exists():  # keep participants.tsv in step
        ensure_raw_dataset(storage, CaseIdentity.from_case(case))
    return _detail(db, case)


@router.delete("/cases/{case_id}", status_code=204)
def delete_case(
    case_id: UUID,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Soft-delete the case and remove its files. Refused while a run is active or when
    the case is flagged keep-forever."""
    load_case(db, case_id, current_user, "case.delete")
    # Serialise with run creation and input changes, then re-check under the lock.
    case = lock_case(db, case_id)
    if case.keep_forever:
        raise v1_error(409, "case_keep_forever", "The case is flagged keep-forever")
    active = db.scalar(
        select(PipelineRun.id)
        .where(PipelineRun.case_id == case.id, PipelineRun.status.in_(ACTIVE_STATUSES))
        .limit(1)
    )
    if active is not None:
        raise v1_error(409, "case_has_active_run", "A run of this case is active")
    case.deleted_at = utcnow()
    audit_event(
        db, "case_deleted", current_user, case_id=case.id, ip_address=client_ip(request)
    )
    db.commit()
    try:
        delete_case_files(
            storage_for(settings), case.id, keep_forever=case.keep_forever
        )
    except (OSError, ValueError, RetentionBlocked):
        log.exception("Could not remove the files of case %s", case.id)
    return Response(status_code=204)


# ---------------------------------------------------------------------- inputs


def _check_nifti(path: Path) -> None:
    """The file is gzip and starts with a NIfTI-1 or NIfTI-2 header. Content is judged by steps."""
    try:
        with gzip.open(path, "rb") as handle:
            head = handle.read(_NIFTI2_HEADER)
    except (OSError, EOFError):
        raise v1_error(
            422, "invalid_input_file", "The file is not a valid .nii.gz"
        ) from None
    ok = False
    for size, magic_at in ((_NIFTI1_HEADER, 344), (_NIFTI2_HEADER, 4)):
        for order in ("little", "big"):
            if len(head) >= size and int.from_bytes(head[:4], order) == size:
                ok = ok or head[magic_at : magic_at + 3] in (b"n+1", b"ni1", b"n+2")
    if not ok:
        raise v1_error(422, "invalid_input_file", "The file is not a NIfTI image")


@router.put(
    "/cases/{case_id}/inputs/{role}", response_model=InputFileRead, status_code=201
)
def put_input(
    case_id: UUID,
    role: str,
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    orch: Orchestration = Depends(get_orchestration),
) -> InputFile:
    """Upload or replace the input of one role (for example ``ct``, ``lesion_native``,
    ``lesion_mni``), before the first run. 201 for a new role, 200 for a replacement."""
    case = load_case(db, case_id, current_user, "case.inputs.write")
    types = input_role_types(orch).get(role)
    if not types:
        raise v1_error(422, "unknown_input_role", f"Unknown input role '{role}'")
    if len(types) > 1:
        raise v1_error(
            422,
            "ambiguous_input_role",
            f"Role '{role}' has different types in different pipelines",
        )
    atype = next(iter(types))
    _version, type_defs = artefact_types()
    type_def = type_defs[atype]
    naming = BidsNaming.from_type(type_def)
    if not (file.filename or "").lower().endswith(naming.extension):
        raise v1_error(
            422,
            "file_extension_not_allowed",
            f"Role '{role}' needs a {naming.extension} file",
        )
    if _has_runs(db, case.id):  # early refusal; re-checked under the lock below
        raise v1_error(409, "case_has_runs", "Inputs cannot change after the first run")

    storage = storage_for(settings)
    # One private temp directory per request, outside every case directory. Only this
    # request's directory is removed; leftovers of a crash are harmless.
    incoming = storage.root / ".incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    tmp_dir = Path(tempfile.mkdtemp(dir=incoming, prefix="up-"))
    tmp = tmp_dir / "upload"
    try:
        size = 0
        hasher = hashlib.sha256()
        with tmp.open("wb") as out:
            while chunk := file.file.read(_CHUNK):
                size += len(chunk)
                if size > settings.max_upload_size_bytes:
                    raise v1_error(
                        413,
                        "upload_too_large",
                        "The file exceeds the maximum allowed size",
                    )
                hasher.update(chunk)
                out.write(chunk)
        if size == 0:
            raise v1_error(422, "invalid_input_file", "The file is empty")
        if type_def.get("format") == "nifti":
            _check_nifti(tmp)

        # Critical section: case row, then owner row. Re-check everything that may
        # have changed during the upload, then swap the file in atomically (the case
        # store writes through a temporary file and renames) and commit before the
        # locks are released.
        case = lock_case(db, case_id)
        if _has_runs(db, case.id):
            raise v1_error(
                409, "case_has_runs", "Inputs cannot change after the first run"
            )
        owner = lock_user(db, case.owner_user_id)
        existing = db.scalar(
            select(InputFile).where(
                InputFile.case_id == case.id, InputFile.role == role
            )
        )
        check_quota(
            db,
            owner,
            size,
            replaced_bytes=(existing.size_bytes or 0) if existing else 0,
        )
        stored = store_raw_input(
            storage,
            CaseIdentity.from_case(case),
            naming,
            tmp,
            expected_sha256=hasher.hexdigest(),
            platform_version=os.environ.get("PLATFORM_VERSION"),
        )
        created = existing is None
        if created:
            existing = InputFile(
                case_id=case.id,
                role=role,
                artefact_type=atype,
                uploaded_by_user_id=current_user.id,
            )
            db.add(existing)
        else:
            response.status_code = 200
            existing.uploaded_by_user_id = current_user.id
            existing.created_at = utcnow()
        existing.storage_path = stored.relative_path
        existing.sha256 = stored.sha256
        existing.size_bytes = stored.size_bytes
        case.updated_at = utcnow()
        audit_event(
            db,
            "input_uploaded",
            current_user,
            case_id=case.id,
            details={
                "role": role,
                "artefact_type": atype,
                "size_bytes": stored.size_bytes,
            },
            ip_address=client_ip(request),
        )
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if created:
                storage.root.joinpath(stored.relative_path).unlink(missing_ok=True)
            raise v1_error(
                409, "input_conflict", "The input was changed by another request"
            ) from None
    except BaseException:
        db.rollback()  # releases the row locks
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    db.refresh(existing)
    return existing


@router.get("/cases/{case_id}/inputs", response_model=list[InputFileRead])
def list_inputs(
    case_id: UUID,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[InputFile]:
    case = load_case(db, case_id, current_user, "case.read")
    return list(
        db.scalars(
            select(InputFile)
            .where(InputFile.case_id == case.id)
            .order_by(InputFile.role)
        )
    )


def _input(db: Session, case: Case, role: str) -> InputFile:
    row = db.scalar(
        select(InputFile).where(InputFile.case_id == case.id, InputFile.role == role)
    )
    if row is None:
        raise v1_error(404, "input_not_found", "Input not found")
    return row


@router.get("/cases/{case_id}/inputs/{role}/download")
def download_input(
    case_id: UUID,
    role: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    case = load_case(db, case_id, current_user, "case.inputs.download")
    row = _input(db, case, role)
    path = case_file(storage_for(settings), case.id, row.storage_path)
    if path is None:
        raise v1_error(
            404, "file_missing_in_storage", "The file does not exist in storage"
        )
    return FileResponse(path, filename=path.name, media_type="application/gzip")


@router.delete("/cases/{case_id}/inputs/{role}", status_code=204)
def delete_input(
    case_id: UUID,
    role: str,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    load_case(db, case_id, current_user, "case.inputs.write")
    case = lock_case(db, case_id)
    if _has_runs(db, case.id):
        raise v1_error(409, "case_has_runs", "Inputs cannot change after the first run")
    row = _input(db, case, role)
    path = case_file(storage_for(settings), case.id, row.storage_path)
    db.delete(row)
    audit_event(
        db,
        "input_deleted",
        current_user,
        case_id=case.id,
        details={"role": role},
        ip_address=client_ip(request),
    )
    db.commit()
    if path is not None:
        path.unlink(missing_ok=True)
    return Response(status_code=204)
