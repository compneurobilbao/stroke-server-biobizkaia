"""Pipelines, their slots and the enabled implementations of each slot (decision D15)."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.deps import Orchestration, authorize, get_orchestration
from app.api.v1.errors import v1_error
from app.db.session import get_db
from app.models.reference_bundle import SlotImplementationSetting
from app.models.user import User
from app.schemas.api_v1 import (
    InputSetRead,
    PipelineDetail,
    PipelineInputRead,
    PipelineSummary,
    PortRead,
    SlotImplementationRead,
    SlotRead,
)
from app.services.auth import get_current_user
from orchestrator.pipeline import PipelineDefinition

router = APIRouter(tags=["pipelines"])

_SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)(?:-(.+))?$")


def version_key(version: str) -> tuple:
    """Sort key: numeric parts, and a release above its own pre-releases."""
    match = _SEMVER.match(version)
    if not match:
        return (0, 0, 0, 0, version)
    major, minor, patch, pre = match.groups()
    return (int(major), int(minor), int(patch), 1 if pre is None else 0, pre or "")


def settings_of(
    db: Session, pipeline_id: str
) -> dict[str, dict[str, SlotImplementationSetting]]:
    out: dict[str, dict[str, SlotImplementationSetting]] = {}
    for row in db.scalars(
        select(SlotImplementationSetting).where(
            SlotImplementationSetting.pipeline_id == pipeline_id
        )
    ):
        out.setdefault(row.slot, {})[row.implementation_id] = row
    return out


def find_pipeline(
    orch: Orchestration, pipeline_id: str, version: str | None
) -> PipelineDefinition:
    if version is not None:
        found = orch.pipelines.get(pipeline_id, version)
    else:
        versions = [p for p in orch.pipelines.all() if p.id == pipeline_id]
        found = max(versions, key=lambda p: version_key(p.version), default=None)
    if found is None:
        raise v1_error(404, "pipeline_not_found", "Pipeline not found")
    return found


def _summary(p: PipelineDefinition) -> dict[str, Any]:
    return {
        "id": p.id,
        "version": p.version,
        "title": p.data["title"],
        "description": p.data.get("description"),
        "sha256": p.sha256,
        "inputs": [
            PipelineInputRead(
                name=i["name"], type=i["type"], description=i.get("description")
            )
            for i in p.data["inputs"]
        ],
        "input_sets": [
            InputSetRead(
                name=s["name"],
                inputs=list(s["inputs"]),
                description=s.get("description"),
            )
            for s in p.data["input_sets"]
        ],
    }


def build_slots(
    pipeline: PipelineDefinition,
    settings: Mapping[str, Mapping[str, SlotImplementationSetting]],
    orch: Orchestration,
    *,
    include_disabled: bool = False,
) -> list[SlotRead]:
    slots = []
    for position, slot in enumerate(pipeline.slots):
        slot_settings = settings.get(slot["name"], {})
        pipeline_default = slot["implementations"].get("default")
        implementations = []
        enabled_ids = []
        for impl in slot["implementations"]["allowed"]:
            setting = slot_settings.get(impl["id"])
            enabled = bool(setting and setting.enabled)
            if enabled:
                enabled_ids.append(impl["id"])
            if not enabled and not include_disabled:
                continue
            manifest = orch.steps.manifest(
                impl["step"]["name"], impl["step"]["version"], impl["image"]
            )
            implementations.append(
                SlotImplementationRead(
                    id=impl["id"],
                    title=(manifest or {}).get("title"),
                    step_name=impl["step"]["name"],
                    step_version=impl["step"]["version"],
                    image=impl["image"],
                    stub=bool(manifest.get("stub", False)) if manifest else None,
                    available=manifest is not None,
                    enabled=enabled,
                    is_default=bool(setting and setting.is_default),
                    is_pipeline_default=impl["id"] == pipeline_default,
                )
            )
        # The rule of pipeline.md section 6 item 3 without a user choice.
        default = next(
            (i for i in enabled_ids if slot_settings[i].is_default),
            pipeline_default if pipeline_default in enabled_ids else None,
        )
        slots.append(
            SlotRead(
                name=slot["name"],
                title=slot.get("title"),
                description=slot.get("description"),
                position=position,
                optional=bool(slot.get("optional", False)),
                when=slot.get("when"),
                consumes=[
                    PortRead(
                        port=c["port"],
                        type=c["type"],
                        optional=bool(c.get("optional", False)),
                    )
                    for c in slot["consumes"]
                ],
                produces=[
                    PortRead(port=p["port"], type=p["type"]) for p in slot["produces"]
                ],
                default_implementation=default,
                implementations=implementations,
            )
        )
    return slots


@router.get("/pipelines", response_model=list[PipelineSummary])
def list_pipelines(
    current_user: User = Depends(get_current_user),
    orch: Orchestration = Depends(get_orchestration),
) -> list[PipelineSummary]:
    authorize(current_user, "pipeline.list")
    ordered = sorted(
        orch.pipelines.all(),
        key=lambda p: (p.id, version_key(p.version)),
        reverse=False,
    )
    return [PipelineSummary(**_summary(p)) for p in ordered]


@router.get(
    "/pipelines/{pipeline_id}/versions/{version}", response_model=PipelineDetail
)
def get_pipeline(
    pipeline_id: str,
    version: str,
    include_disabled: bool = Query(False, description="Administrators only."),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Orchestration = Depends(get_orchestration),
) -> PipelineDetail:
    authorize(current_user, "pipeline.list")
    pipeline = _visible(pipeline_id, version, current_user, include_disabled, orch)
    slots = build_slots(
        pipeline,
        settings_of(db, pipeline.id),
        orch,
        include_disabled=include_disabled,
    )
    return PipelineDetail(**_summary(pipeline), slots=slots)


@router.get(
    "/pipelines/{pipeline_id}/versions/{version}/slots", response_model=list[SlotRead]
)
def list_slots(
    pipeline_id: str,
    version: str,
    include_disabled: bool = Query(False, description="Administrators only."),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Orchestration = Depends(get_orchestration),
) -> list[SlotRead]:
    """Slots of a pipeline with their enabled implementations and the default of each."""
    authorize(current_user, "pipeline.list")
    pipeline = _visible(pipeline_id, version, current_user, include_disabled, orch)
    return build_slots(
        pipeline, settings_of(db, pipeline.id), orch, include_disabled=include_disabled
    )


def _visible(pipeline_id, version, user, include_disabled, orch) -> PipelineDefinition:
    if include_disabled:
        authorize(user, "implementation.manage")
    return find_pipeline(orch, pipeline_id, version)
