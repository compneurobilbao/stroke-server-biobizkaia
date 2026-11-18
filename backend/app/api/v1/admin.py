"""Administrator endpoints: enable, disable and default implementations of pipeline slots
and presets.

Settings are stored per pipeline id (``slot_implementation_settings``,
``preset_settings``), not per version, and apply to every loaded version that allows the
implementation or offers the preset.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    Orchestration,
    audit_event,
    authorize,
    effective_min_validation,
    get_orchestration,
)
from app.api.v1.errors import v1_error
from app.api.v1.pipelines import version_key
from app.core.validation import NOT_VALIDATED_CODE, below_minimum
from app.db.session import get_db
from app.models._common import utcnow
from app.models.preset_setting import PresetSetting
from app.models.reference_bundle import SlotImplementationSetting
from app.models.user import User
from app.schemas.api_v1 import (
    ImplementationAdminRead,
    PresetAdminRead,
    PresetSettingUpdate,
)
from app.schemas.bundle import SlotImplementationSettingUpdate
from app.services.auth import client_ip, get_current_user

router = APIRouter(prefix="/admin", tags=["admin"])


def _allowed(orch: Orchestration):
    """(pipeline id, slot, implementation id) -> (step name, version, image), newest version first."""
    out: dict[tuple[str, str, str], tuple[str, str, str]] = {}
    for pipeline in sorted(
        orch.pipelines.all(), key=lambda p: version_key(p.version), reverse=True
    ):
        for slot in pipeline.slots:
            for impl in slot["implementations"]["allowed"]:
                key = (pipeline.id, slot["name"], impl["id"])
                out.setdefault(
                    key,
                    (impl["step"]["name"], impl["step"]["version"], impl["image"]),
                )
    return out


def _read(
    key,
    step,
    row: SlotImplementationSetting | None,
    level: str | None = None,
    below_minimum_: bool = False,
) -> ImplementationAdminRead:
    return ImplementationAdminRead(
        pipeline_id=key[0],
        slot=key[1],
        implementation_id=key[2],
        step_name=step[0],
        step_version=step[1],
        image=step[2],
        enabled=bool(row and row.enabled),
        is_default=bool(row and row.is_default),
        validation_level=level,
        experimental=level == "reference-tested",
        below_minimum=below_minimum_,
        updated_at=row.updated_at if row else None,
        updated_by_user_id=row.updated_by_user_id if row else None,
    )


@router.get("/implementations", response_model=list[ImplementationAdminRead])
def list_implementations(
    pipeline_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Orchestration = Depends(get_orchestration),
) -> list[ImplementationAdminRead]:
    """Every implementation allowed by a loaded pipeline, with its deployment setting.
    An implementation without a setting row is disabled."""
    authorize(current_user, "implementation.manage")
    rows = {
        (r.pipeline_id, r.slot, r.implementation_id): r
        for r in db.scalars(select(SlotImplementationSetting))
    }
    minimum = effective_min_validation()
    out = []
    for key, step in sorted(_allowed(orch).items()):
        if pipeline_id is not None and key[0] != pipeline_id:
            continue
        level = orch.validation.level(*key)
        out.append(
            _read(key, step, rows.get(key), level, below_minimum(level, minimum))
        )
    return out


@router.put(
    "/implementations/{pipeline_id}/{slot}/{implementation_id}",
    response_model=ImplementationAdminRead,
)
def set_implementation(
    pipeline_id: str,
    slot: str,
    implementation_id: str,
    body: SlotImplementationSettingUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Orchestration = Depends(get_orchestration),
) -> ImplementationAdminRead:
    """Enable or disable an implementation and optionally make it the slot's default.
    Making one the default clears the default of the slot's other implementations."""
    authorize(current_user, "implementation.manage")
    key = (pipeline_id, slot, implementation_id)
    step = _allowed(orch).get(key)
    if step is None:
        raise v1_error(
            404,
            "implementation_not_found",
            "No loaded pipeline allows this implementation in this slot",
        )
    if body.enabled:
        # Owner decision 8: nothing below the deployment's minimum level is enabled.
        # Disabling is never refused.
        level = orch.validation.level(pipeline_id, slot, implementation_id)
        minimum = effective_min_validation()
        if below_minimum(level, minimum):
            raise v1_error(
                422,
                NOT_VALIDATED_CODE,
                f"This implementation is {level}; this deployment enables only "
                f"implementations that are at least {minimum}",
                slot,
            )
    now = utcnow()
    if body.is_default:
        db.execute(
            update(SlotImplementationSetting)
            .where(
                SlotImplementationSetting.pipeline_id == pipeline_id,
                SlotImplementationSetting.slot == slot,
                SlotImplementationSetting.implementation_id != implementation_id,
                SlotImplementationSetting.is_default.is_(True),
            )
            .values(
                is_default=False, updated_at=now, updated_by_user_id=current_user.id
            )
            .execution_options(synchronize_session=False)
        )
    row = db.scalar(
        select(SlotImplementationSetting).where(
            SlotImplementationSetting.pipeline_id == pipeline_id,
            SlotImplementationSetting.slot == slot,
            SlotImplementationSetting.implementation_id == implementation_id,
        )
    )
    if row is None:
        row = SlotImplementationSetting(
            pipeline_id=pipeline_id, slot=slot, implementation_id=implementation_id
        )
        db.add(row)
    row.enabled = body.enabled
    row.is_default = body.is_default
    row.updated_at = now
    row.updated_by_user_id = current_user.id
    audit_event(
        db,
        "implementation_setting_changed",
        current_user,
        details={
            "pipeline_id": pipeline_id,
            "slot": slot,
            "implementation_id": implementation_id,
            "enabled": body.enabled,
            "is_default": body.is_default,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(row)
    level = orch.validation.level(*key)
    return _read(key, step, row, level, below_minimum(level, effective_min_validation()))


# ----------------------------------------------------------------------- presets


def _loaded_presets(orch: Orchestration) -> dict[tuple[str, str], dict]:
    """(pipeline id, preset id) -> title, preset versions and the loaded pipeline
    versions that offer it, from every loaded pipeline version."""
    out: dict[tuple[str, str], dict] = {}
    for pipeline in sorted(
        orch.pipelines.all(), key=lambda p: version_key(p.version), reverse=True
    ):
        for preset in orch.presets.presets(pipeline.id, pipeline.version):
            entry = out.setdefault(
                (pipeline.id, preset.id),
                {"title": preset.data["title"], "versions": set(), "pipelines": set()},
            )
            entry["versions"].add(preset.version)
            entry["pipelines"].add(pipeline.version)
    return out


def _preset_read(key, entry, row: PresetSetting | None) -> PresetAdminRead:
    return PresetAdminRead(
        pipeline_id=key[0],
        preset_id=key[1],
        title=entry["title"],
        versions=sorted(entry["versions"], key=version_key),
        pipeline_versions=sorted(entry["pipelines"], key=version_key),
        enabled=bool(row and row.enabled),
        is_default=bool(row and row.is_default),
        updated_at=row.updated_at if row else None,
        updated_by_user_id=row.updated_by_user_id if row else None,
    )


@router.get("/presets", response_model=list[PresetAdminRead])
def list_presets(
    pipeline_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Orchestration = Depends(get_orchestration),
) -> list[PresetAdminRead]:
    """Every preset offered by a loaded pipeline version, with its deployment setting.
    A preset without a setting row is disabled."""
    authorize(current_user, "preset.manage")
    rows = {(r.pipeline_id, r.preset_id): r for r in db.scalars(select(PresetSetting))}
    return [
        _preset_read(key, entry, rows.get(key))
        for key, entry in sorted(_loaded_presets(orch).items())
        if pipeline_id is None or key[0] == pipeline_id
    ]


@router.put("/presets/{pipeline_id}/{preset_id}", response_model=PresetAdminRead)
def set_preset(
    pipeline_id: str,
    preset_id: str,
    body: PresetSettingUpdate,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Orchestration = Depends(get_orchestration),
) -> PresetAdminRead:
    """Enable or disable a preset and optionally make it the pipeline's default (at most
    one; it replaces the previous one). Disabling only hides the preset from new runs:
    runs already created keep their plan and can be resumed."""
    authorize(current_user, "preset.manage")
    key = (pipeline_id, preset_id)
    entry = _loaded_presets(orch).get(key)
    if entry is None:
        raise v1_error(
            404,
            "preset_not_found",
            "No loaded pipeline version offers this preset",
        )
    now = utcnow()
    if body.is_default:
        db.execute(
            update(PresetSetting)
            .where(
                PresetSetting.pipeline_id == pipeline_id,
                PresetSetting.preset_id != preset_id,
                PresetSetting.is_default.is_(True),
            )
            .values(
                is_default=False, updated_at=now, updated_by_user_id=current_user.id
            )
            .execution_options(synchronize_session=False)
        )
    row = db.scalar(
        select(PresetSetting).where(
            PresetSetting.pipeline_id == pipeline_id,
            PresetSetting.preset_id == preset_id,
        )
    )
    if row is None:
        row = PresetSetting(pipeline_id=pipeline_id, preset_id=preset_id)
        db.add(row)
    row.enabled = body.enabled
    row.is_default = body.is_default
    row.updated_at = now
    row.updated_by_user_id = current_user.id
    audit_event(
        db,
        "preset_setting_changed",
        current_user,
        details={
            "pipeline_id": pipeline_id,
            "preset_id": preset_id,
            "enabled": body.enabled,
            "is_default": body.is_default,
        },
        ip_address=client_ip(request),
    )
    db.commit()
    db.refresh(row)
    return _preset_read(key, entry, row)
