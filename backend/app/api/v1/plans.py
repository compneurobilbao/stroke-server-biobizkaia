"""Presets, plan preview and the caller's configuration (run-plans.md 2.3, 3.5, 3.8; card F1-21).

Preview and run creation answer from the same code: ``orchestrator.planning``
(``resolve_request``, ``plan``) with the deployment's settings
(``orchestrator.resolution.enablement``), followed by the check of
``provenance_consumer_reasons`` (``step_contract_too_old``). ``evaluate`` below is that
sequence; it reads the database and writes nothing. What preview cannot know is what
depends on the case or on a step image at creation: effective parameters, bundles and
manifests are checked by ``create_run`` only.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    Orchestration,
    authorize,
    deployment_profile,
    effective_run_plan_policy,
    get_orchestration,
    require_free_mode,
)
from app.api.v1.errors import v1_error
from app.api.v1.pipelines import find_pipeline
from app.core.config import free_mode_allowed
from app.db.session import get_db
from app.models.preset_setting import PresetSetting
from app.models.user import User
from app.schemas.api_v1 import (
    ConfigRead,
    PlanPresetRead,
    PlanPreviewRead,
    PlanReasonRead,
    PlanRequestIn,
    PlanSlotRead,
    PlanTargetsRead,
    PresetChoiceRead,
    PresetPlanRead,
    PresetRead,
)
from app.services.auth import get_current_user
from contracts.validate import PlanReason
from orchestrator.catalog import PresetDefinition, StepCatalog
from orchestrator.pipeline import PipelineDefinition, PipelineError, sources_of
from orchestrator.planning import (
    SELECTED,
    Enablement,
    Plan,
    PlanRefused,
    PlanRequest,
    PresetRef,
    SlotDecision,
    TargetRequest,
    plan,
    provenance_consumer_reasons,
    reason_dict,
    resolve_request,
)
from orchestrator.resolution import enablement

router = APIRouter(tags=["plans"])

_ROLE_RANK = {"target": 0, "upstream": 1, "if_available": 2}


@dataclass(frozen=True)
class Evaluation:
    """The outcome of a plan request: feasible when ``reasons`` is empty. ``slots`` is
    empty when the request was refused before a plan could be computed."""

    mode: str
    input_set: str
    preset: PresetDefinition | None
    targets: dict
    slots: tuple[SlotDecision, ...]
    reasons: tuple[PlanReason, ...]

    @property
    def feasible(self) -> bool:
        return not self.reasons


def evaluate(
    pipeline: PipelineDefinition,
    input_set: str,
    request: PlanRequest | PlanRefused,
    deployment: Enablement,
    steps: StepCatalog,
) -> Evaluation:
    """Plan ``request`` (the result of ``resolve_request``) as ``create_run`` does."""
    if isinstance(request, PlanRefused):
        return Evaluation(request.mode, input_set, None, {}, (), request.reasons)
    result = plan(pipeline, input_set, request, deployment)
    if isinstance(result, PlanRefused):
        return Evaluation(
            result.mode, input_set, request.preset, {}, result.slots, result.reasons
        )
    return Evaluation(
        result.mode,
        input_set,
        result.preset,
        result.targets,
        result.slots,
        tuple(_contract_reasons(pipeline, result, steps)),
    )


def _contract_reasons(
    pipeline: PipelineDefinition, result: Plan, steps: StepCatalog
) -> list[PlanReason]:
    manifests = {}
    for decision in result.selected:
        impl = pipeline.implementation(decision.slot, decision.implementation_id)
        manifest = steps.manifest(
            impl["step"]["name"], impl["step"]["version"], impl["image"]
        )
        if manifest is not None:
            manifests[decision.slot] = manifest
    return provenance_consumer_reasons(pipeline, result, manifests)


def required_by(
    pipeline: PipelineDefinition, slots: tuple[SlotDecision, ...]
) -> dict[str, str]:
    """For each planned slot that is not a target, the planned slot that needs it:
    one that consumes a non-optional port the slot produces, preferring targets, then
    upstream slots, then others, in dependency order. Derived from the wiring."""
    planned = {s.slot: s for s in slots if s.plan_role is not None}
    order = {name: i for i, name in enumerate(pipeline.topological_order)}
    out: dict[str, str] = {}
    for name, decision in planned.items():
        if decision.plan_role == "target":
            continue
        consumers = []
        for other, other_decision in planned.items():
            if other == name or other_decision.status != SELECTED:
                continue
            for consume in pipeline.slot(other)["consumes"]:
                if consume.get("optional", False):
                    continue
                if any(
                    s.kind == "slot" and s.name == name for s in sources_of(consume)
                ):
                    consumers.append(other)
                    break
        if consumers:
            consumers.sort(key=lambda c: (_ROLE_RANK[planned[c].plan_role], order[c]))
            out[name] = consumers[0]
    return out


def slot_reads(
    pipeline: PipelineDefinition, slots: tuple[SlotDecision, ...]
) -> list[PlanSlotRead]:
    needed = required_by(pipeline, slots)
    return [
        PlanSlotRead(
            name=d.slot,
            status=d.status,
            reason=d.reason,
            plan_role=d.plan_role,
            required_by=needed.get(d.slot),
            implementation=d.implementation_id,
            chosen_by=d.chosen_by,
        )
        for d in slots
    ]


def reason_reads(reasons) -> list[PlanReasonRead]:
    return [PlanReasonRead(**reason_dict(r)) for r in reasons]


def preset_ref_read(preset: PresetDefinition | None) -> PlanPresetRead | None:
    if preset is None:
        return None
    return PlanPresetRead(id=preset.id, version=preset.version, sha256=preset.sha256)


def targets_read(targets: dict) -> PlanTargetsRead:
    return PlanTargetsRead(
        required=list(targets.get("required", [])),
        if_available=list(targets.get("if_available", [])),
    )


def validate_request_roles(pipeline: PipelineDefinition, roles, implementations) -> str:
    """The checks of ``create_run`` that come before planning; returns the input set."""
    unknown = set(implementations) - {s["name"] for s in pipeline.slots}
    if unknown:
        raise v1_error(422, "unknown_slot", f"unknown slots: {sorted(unknown)}")
    for role in roles:
        if role not in pipeline.inputs:
            raise v1_error(
                422,
                "input_type_mismatch",
                f"input '{role}' does not match the pipeline inputs",
            )
    try:
        return pipeline.match_input_set(roles)
    except PipelineError as exc:
        raise v1_error(422, "no_matching_input_set", str(exc)) from None


# --------------------------------------------------------------------- endpoints


@router.get(
    "/pipelines/{pipeline_id}/versions/{version}/presets",
    response_model=list[PresetRead],
)
def list_presets(
    pipeline_id: str,
    version: str,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Orchestration = Depends(get_orchestration),
) -> list[PresetRead]:
    """Presets attached to this pipeline version that are enabled in the deployment:
    title, accepted input sets, the plan per input set (slots, roles, implementations)
    and ``available`` with the reasons when the deployment cannot run it now (for
    example a required slot has no enabled implementation). Several versions of one
    preset are all listed, ascending."""
    authorize(current_user, "pipeline.list")
    pipeline = find_pipeline(orch, pipeline_id, version)
    deployment = enablement(db, pipeline.id)
    defaults = {
        row.preset_id
        for row in db.scalars(
            select(PresetSetting).where(
                PresetSetting.pipeline_id == pipeline.id, PresetSetting.is_default
            )
        )
    }
    out = []
    for preset in orch.presets.presets(pipeline.id, pipeline.version):
        if preset.id not in deployment.presets:
            continue
        plans = []
        for input_set in preset.input_sets:
            evaluation = evaluate(
                pipeline,
                input_set,
                PlanRequest(preset=preset),
                deployment,
                orch.steps,
            )
            plans.append(
                PresetPlanRead(
                    input_set=input_set,
                    available=evaluation.feasible,
                    slots=slot_reads(pipeline, evaluation.slots),
                    reasons=reason_reads(evaluation.reasons),
                )
            )
        out.append(
            PresetRead(
                id=preset.id,
                version=preset.version,
                sha256=preset.sha256,
                title=preset.data["title"],
                description=preset.data.get("description"),
                input_sets=preset.input_sets,
                targets=PlanTargetsRead(
                    required=preset.required, if_available=preset.if_available
                ),
                implementations={
                    slot: PresetChoiceRead(**entry)
                    for slot, entry in preset.data.get("implementations", {}).items()
                },
                is_default=preset.id in defaults,
                available=any(p.available for p in plans),
                plans=plans,
            )
        )
    return out


@router.post(
    "/pipelines/{pipeline_id}/versions/{version}/plan",
    response_model=PlanPreviewRead,
)
def preview_plan(
    pipeline_id: str,
    version: str,
    body: PlanRequestIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    orch: Orchestration = Depends(get_orchestration),
) -> PlanPreviewRead:
    """Dry run of run creation: the plan the request would get, with the same policy
    checks (403 ``free_mode_not_allowed`` for targets when the policy does not allow
    them to this user) and the same plan as ``POST /cases/{id}/runs`` for the same
    request. A refused plan is a 200 with ``feasible: false`` and every reason; only a
    malformed request (unknown slot or input role, inputs matching no input set) is a
    422. Nothing is written."""
    authorize(current_user, "run.create")
    if body.targets is not None:
        require_free_mode(current_user)
    pipeline = find_pipeline(orch, pipeline_id, version)
    input_set = validate_request_roles(pipeline, body.input_roles, body.implementations)
    request = resolve_request(
        pipeline,
        preset=PresetRef(body.preset.id, body.preset.version)
        if body.preset is not None
        else None,
        targets=(
            TargetRequest(
                tuple(body.targets.required), tuple(body.targets.if_available)
            )
            if body.targets is not None
            else None
        ),
        implementations=body.implementations,
        presets=orch.presets,
    )
    evaluation = evaluate(
        pipeline, input_set, request, enablement(db, pipeline.id), orch.steps
    )
    return PlanPreviewRead(
        feasible=evaluation.feasible,
        mode=evaluation.mode,
        input_set=input_set,
        preset=preset_ref_read(evaluation.preset),
        targets=targets_read(evaluation.targets),
        slots=slot_reads(pipeline, evaluation.slots),
        reasons=reason_reads(evaluation.reasons),
    )


@router.get("/config", response_model=ConfigRead)
def get_config(current_user: User = Depends(get_current_user)) -> ConfigRead:
    """The deployment profile, its run-plan policy and whether the caller may use free
    mode, so that the UI does not offer it in vain."""
    policy = effective_run_plan_policy()
    return ConfigRead(
        deployment_profile=deployment_profile(),
        run_plan_policy=policy,
        free_mode_allowed=free_mode_allowed(policy, current_user.role),
    )
