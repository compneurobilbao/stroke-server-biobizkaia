"""Run creation: resolve a pipeline and a plan request against a case
(pipeline.md section 6, presets.md 4-5; cards F0-15 and F1-19).

``create_run`` is the "orchestrator's resolution function" of docs/data-model.md
section 8. The run API (F0-30) calls it inside its own transaction, commits, and
then enqueues ``worker.tasks.run_pipeline``. The plan (which slots run, with which
implementation, and why) comes from the pure function
``orchestrator.planning.plan``; this module reads the deployment's settings,
materialises the selected slots (manifests, parameters, bundles) and writes,
without committing:

- one ``pipeline_runs`` row in ``queued`` with the plan request as resolved
  (``plan_mode``, ``preset_id``/``preset_version``/``preset_sha256``, ``targets``);
- one ``run_inputs`` row per role of the matched input set;
- one ``run_slots`` row per pipeline slot, in pipeline order, with status,
  reason, ``plan_role``, chosen implementation, ``chosen_by``, the frozen
  effective parameters and the manifest's ``upstream`` block;
- one ``run_slot_bundles`` row per resolved bundle requirement.

A request with neither a preset nor targets is the full plan (rule R0): the same
rows and the same refusals (code and slot) as before plans existed. It raises
``RunRefused`` (with a snake_case ``code`` and, for plan refusals, every reason in
``reasons``) when the run cannot be created; nothing is written in that case (the
caller rolls back).
"""

from __future__ import annotations

import copy
import secrets
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.case import Case, InputFile
from app.models.pipeline_run import (
    PipelineRun,
    RunInput,
    RunSlot,
    RunSlotBundle,
    RunStatus,
    SlotStatus,
)
from app.models.preset_setting import PresetSetting
from app.models.reference_bundle import ReferenceBundle, SlotImplementationSetting
from contracts.validate import PlanReason
from orchestrator.bundle_resolver import BundleError, BundleResolver
from orchestrator.catalog import PresetRegistry, StepCatalog
from orchestrator.executors.base import Accounting
from orchestrator.pipeline import (
    PipelineDefinition,
    PipelineError,
    artefact_types,
    check_implementation,
    condition_holds,
    sources_of,
    step_identity_errors,
)
from orchestrator.planning import (
    FULL,
    Enablement,
    Plan,
    PlanRefused,
    PresetRef,
    TargetRequest,
    plan,
    provenance_consumer_reasons,
    resolve_request,
    targets_errors,
)

SEED_PARAMETER = "seed"
BINDING_SOURCES = {"case.sex": "sex", "case.age_band": "age_band"}


class RunRefused(Exception):
    """Run creation refused. ``code`` is stable and snake_case; ``slot`` when relevant;
    ``reasons`` lists every reason of a refused plan (run-plans.md 3.3), the first of
    which gives ``code`` and ``slot``."""

    def __init__(
        self,
        code: str,
        message: str,
        slot: str | None = None,
        reasons: Iterable[PlanReason] = (),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.slot = slot
        self.reasons = tuple(reasons)

    @classmethod
    def from_plan(cls, refused: PlanRefused) -> RunRefused:
        return cls(refused.code, refused.message, refused.slot, refused.reasons)


def effective_parameters(
    manifest: Mapping[str, Any],
    implementation: Mapping[str, Any],
    case: Case,
    seed: int,
) -> dict[str, Any]:
    """Defaults, then literals, then known bindings, then the run seed (step-contract.md 1.4)."""
    schema = manifest["parameters"]
    props = schema.get("properties", {})
    values = {name: prop.get("default") for name, prop in props.items()}
    literals = dict(implementation.get("parameters", {}))
    values.update(literals)
    for name, source in implementation.get("parameter_bindings", {}).items():
        value = getattr(case, BINDING_SOURCES[source], None)
        if value is not None:
            values[name] = value
    if SEED_PARAMETER in props and SEED_PARAMETER not in literals:
        values[SEED_PARAMETER] = seed
    errors = [e.message for e in Draft202012Validator(schema).iter_errors(values)]
    if errors:
        raise RunRefused(
            "invalid_parameters",
            f"effective parameters of {manifest['name']} are invalid: {'; '.join(errors)}",
        )
    return values


def enablement(db: Session, pipeline_id: str) -> Enablement:
    """The deployment's implementation and preset settings for one pipeline id."""
    return Enablement.from_settings(
        db.scalars(
            select(SlotImplementationSetting).where(
                SlotImplementationSetting.pipeline_id == pipeline_id
            )
        ).all(),
        db.scalars(
            select(PresetSetting).where(PresetSetting.pipeline_id == pipeline_id)
        ).all(),
    )


def recorded_preset_digests(db: Session) -> dict[tuple[str, str, str], str]:
    """SHA-256 recorded by earlier runs per (pipeline id, preset id, preset version),
    for the P8 check of ``orchestrator.catalog.StaticPresetRegistry`` (a changed file
    with an unchanged version is refused)."""
    rows = db.execute(
        select(
            PipelineRun.pipeline_id,
            PipelineRun.preset_id,
            PipelineRun.preset_version,
            PipelineRun.preset_sha256,
        )
        .where(PipelineRun.preset_id.is_not(None))
        .distinct()
    ).all()
    return {(p, i, v): sha for p, i, v, sha in rows}


def create_run(
    db: Session,
    *,
    case: Case,
    pipeline: PipelineDefinition,
    inputs: Mapping[str, InputFile],
    created_by_user_id: uuid.UUID,
    steps: StepCatalog,
    bundles: BundleResolver,
    accounting: Accounting,
    platform_version: str,
    requested_implementations: Mapping[str, str] | None = None,
    seed: int | None = None,
    preset: PresetRef | None = None,
    targets: TargetRequest | None = None,
    presets: PresetRegistry | None = None,
) -> PipelineRun:
    """Resolve and insert a queued run (see the module docstring). Does not commit.

    ``preset`` names a preset attached to ``pipeline`` in ``presets``; ``targets`` are
    free-mode targets; with neither, the run gets the full plan. Who may use free
    mode is decided by the API (``RUN_PLAN_POLICY``, card F1-21), not here.
    """
    # load_pipeline refuses this already; checked again because registries may
    # build PipelineDefinition objects themselves (card F0-33).
    shared = step_identity_errors(pipeline.data)
    if shared:
        raise RunRefused("step_shared_by_slots", "; ".join(shared))
    requested = dict(requested_implementations or {})
    unknown = set(requested) - {s["name"] for s in pipeline.slots}
    if unknown:
        raise RunRefused("unknown_slot", f"unknown slots: {sorted(unknown)}")
    for role, input_file in inputs.items():
        if input_file.case_id != case.id:
            raise RunRefused(
                "input_not_in_case", f"input '{role}' belongs to another case"
            )
        expected = pipeline.inputs.get(role)
        if expected is None or input_file.artefact_type != expected:
            raise RunRefused(
                "input_type_mismatch",
                f"input '{role}' does not match the pipeline inputs",
            )
    try:
        input_set = pipeline.match_input_set(inputs)
    except PipelineError as exc:
        raise RunRefused("no_matching_input_set", str(exc)) from exc

    request = resolve_request(
        pipeline,
        preset=preset,
        targets=targets,
        implementations=requested,
        presets=presets,
    )
    if isinstance(request, PlanRefused):
        raise RunRefused.from_plan(request)
    result = plan(pipeline, input_set, request, enablement(db, pipeline.id))
    refused = result if isinstance(result, PlanRefused) else None
    if refused is not None and (refused.mode != FULL or not refused.slots):
        raise RunRefused.from_plan(refused)
    # A refused full plan keeps the refusal order of run creation before plans
    # existed: the selected slots before the failing one (``refused.slot``, in
    # dependency order) are materialised first, so that their own refusals
    # (manifest, parameters, bundles) still come first.
    decisions = {d.slot: d for d in result.slots}
    seed = secrets.randbelow(2**32) if seed is None else seed
    types_version, _types = artefact_types()

    run = PipelineRun(
        id=uuid.uuid4(),
        case_id=case.id,
        created_by_user_id=created_by_user_id,
        pipeline_id=pipeline.id,
        pipeline_version=pipeline.version,
        pipeline_sha256=pipeline.sha256,
        platform_version=platform_version,
        artefact_types_version=types_version,
        input_set=input_set,
        seed=seed,
        status=RunStatus.queued.value,
        requested_implementations=requested,
        accounting_submitted_by=accounting.submitted_by,
        accounting_submit_as=accounting.submit_as.value,
        accounting_billing_account=accounting.billing_account,
    )
    if refused is None:
        _record_plan(run, result)
    db.add(run)
    for role, input_file in inputs.items():
        db.add(RunInput(pipeline_run_id=run.id, role=role, input_file_id=input_file.id))

    rows: dict[str, RunSlot] = {}
    manifests: dict[str, Mapping[str, Any]] = {}
    positions = {s["name"]: i for i, s in enumerate(pipeline.slots)}
    for name in pipeline.topological_order:
        if refused is not None and name == refused.slot:
            raise RunRefused.from_plan(refused)
        slot = pipeline.slot(name)
        decision = decisions[name]
        row = RunSlot(
            id=uuid.uuid4(),
            pipeline_run_id=run.id,
            slot=name,
            position=positions[name],
            optional=bool(slot.get("optional", False)),
            status=decision.status,
            reason=decision.reason,
            plan_role=decision.plan_role,
        )
        rows[name] = row
        if decision.status != SlotStatus.selected.value:
            continue
        impl = pipeline.implementation(name, decision.implementation_id)
        manifest = steps.manifest(
            impl["step"]["name"], impl["step"]["version"], impl["image"]
        )
        if manifest is None:
            raise RunRefused(
                "manifest_unavailable",
                f"no manifest for step {impl['step']['name']} {impl['step']['version']}",
                slot=name,
            )
        try:
            check_implementation(pipeline, name, decision.implementation_id, manifest)
        except PipelineError as exc:
            raise RunRefused("implementation_invalid", str(exc), slot=name) from exc
        row.implementation_id = decision.implementation_id
        row.step_name = manifest["name"]
        row.step_version = manifest["version"]
        row.image = impl["image"]
        row.stub = bool(manifest.get("stub", False))
        row.chosen_by = decision.chosen_by
        row.effective_parameters = effective_parameters(manifest, impl, case, seed)
        if manifest.get("upstream") is not None:
            # Copied so that provenance can be regenerated from the database
            # (data-model.md 8); provenance.md 3.1: present exactly when the
            # manifest has one.
            row.upstream = copy.deepcopy(dict(manifest["upstream"]))
        _resolve_bundles(db, row, manifest, impl, bundles)
        manifests[name] = manifest
    if refused is not None:  # the failing slot is not in the pipeline: not expected
        raise RunRefused.from_plan(refused)

    readers = provenance_consumer_reasons(pipeline, result, manifests)
    if readers:
        raise RunRefused(
            readers[0].code,
            " ".join(r.message for r in readers),
            readers[0].slot,
            readers,
        )
    for name in sorted(rows, key=positions.get):
        db.add(rows[name])
    db.flush()
    return run


def _record_plan(run: PipelineRun, result: Plan) -> None:
    """The plan request as resolved, on ``pipeline_runs`` (run-plans.md 3.4)."""
    problems = targets_errors(result.targets)
    if problems:  # the closure builds them; this is a programming error
        raise RuntimeError(f"resolved targets are malformed: {problems}")
    run.plan_mode = result.mode
    run.targets = copy.deepcopy(result.targets)
    if result.preset is not None:
        run.preset_id = result.preset.id
        run.preset_version = result.preset.version
        run.preset_sha256 = result.preset.sha256


# The run resolution as it was before run plans (pipeline.md 6 items 1-4). Kept as
# the reference that rule R0 is tested against (tests/test_contracts.py,
# tests/test_planning.py); ``create_run`` no longer uses it.


def _required_ports_available(
    slot: Mapping[str, Any],
    present: set[str],
    selected: Mapping[str, Mapping[str, Any]],
) -> bool:
    for consume in slot["consumes"]:
        if consume.get("optional", False):
            continue
        if not any(
            _source_possible(src, present, selected) for src in sources_of(consume)
        ):
            return False
    return True


def _source_possible(src, present, selected) -> bool:
    if src.kind == "input":
        return src.name in present
    if src.kind == "run":
        return True
    upstream = selected.get(src.name)
    if upstream is None:
        return False
    port = next(p for p in upstream["produces"] if p["port"] == src.port)
    return condition_holds(port.get("when"), present)


def _resolve_bundles(
    db, row: RunSlot, manifest, impl, resolver: BundleResolver
) -> None:
    requirements = list(manifest.get("bundles") or [])
    if not requirements:
        return
    pins = {
        name: (pin["id"], pin["version"])
        for name, pin in (impl.get("bundles") or {}).items()
    }
    try:
        resolved = resolver.resolve(requirements, pins)
    except BundleError as exc:
        raise RunRefused("bundle_unavailable", str(exc), slot=row.slot) from exc
    for name, bundle in resolved.items():
        ref_id = db.scalar(
            select(ReferenceBundle.id).where(
                ReferenceBundle.bundle_id == bundle.bundle_id,
                ReferenceBundle.version == bundle.version,
            )
        )
        if ref_id is None:
            raise RunRefused(
                "bundle_unavailable",
                f"bundle {bundle.bundle_id} {bundle.version} is not registered in the database",
                slot=row.slot,
            )
        row.bundles.append(
            RunSlotBundle(requirement_name=name, reference_bundle_id=ref_id)
        )
