"""Run creation: resolve a pipeline against a case (pipeline.md section 6, card F0-15).

``create_run`` is the "orchestrator's resolution function" of docs/data-model.md
section 8. The run API (F0-30) calls it inside its own transaction, commits, and
then enqueues ``worker.tasks.run_pipeline``. It writes, without committing:

- one ``pipeline_runs`` row in ``queued``;
- one ``run_inputs`` row per role of the matched input set;
- one ``run_slots`` row per pipeline slot, in pipeline order, with status,
  reason, chosen implementation, ``chosen_by`` and the frozen effective
  parameters;
- one ``run_slot_bundles`` row per resolved bundle requirement.

It raises ``RunRefused`` (with a snake_case ``code``) when the run cannot be
created; nothing is written in that case (the caller rolls back).
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Mapping
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.case import Case, InputFile
from app.models.pipeline_run import (
    ChosenBy,
    PipelineRun,
    RunInput,
    RunSlot,
    RunSlotBundle,
    RunStatus,
    SlotReason,
    SlotStatus,
)
from app.models.reference_bundle import ReferenceBundle, SlotImplementationSetting
from orchestrator.bundle_resolver import BundleError, BundleResolver
from orchestrator.catalog import StepCatalog
from orchestrator.executors.base import Accounting
from orchestrator.pipeline import (
    PipelineDefinition,
    PipelineError,
    artefact_types,
    check_implementation,
    condition_holds,
    sources_of,
)

SEED_PARAMETER = "seed"
BINDING_SOURCES = {"case.sex": "sex", "case.age_band": "age_band"}


class RunRefused(Exception):
    """Run creation refused. ``code`` is stable and snake_case; ``slot`` when relevant."""

    def __init__(self, code: str, message: str, slot: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.slot = slot


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


def _settings(
    db: Session, pipeline_id: str
) -> dict[str, dict[str, SlotImplementationSetting]]:
    rows = db.scalars(
        select(SlotImplementationSetting).where(
            SlotImplementationSetting.pipeline_id == pipeline_id
        )
    ).all()
    out: dict[str, dict[str, SlotImplementationSetting]] = {}
    for row in rows:
        out.setdefault(row.slot, {})[row.implementation_id] = row
    return out


def choose_implementation(
    slot: Mapping[str, Any],
    settings: Mapping[str, SlotImplementationSetting],
    requested: str | None,
) -> tuple[str, str] | None:
    """(implementation id, chosen_by) by the rules of pipeline.md 6 item 3, or None."""
    allowed = [a["id"] for a in slot["implementations"]["allowed"]]
    enabled = [i for i in allowed if i in settings and settings[i].enabled]
    if requested is not None:
        if requested not in allowed or requested not in enabled:
            raise RunRefused(
                "implementation_not_enabled",
                f"implementation '{requested}' is not enabled for slot '{slot['name']}'",
                slot=slot["name"],
            )
        return requested, ChosenBy.user.value
    defaults = [i for i in enabled if settings[i].is_default]
    if defaults:
        return defaults[0], ChosenBy.deployment_default.value
    pipeline_default = slot["implementations"].get("default")
    if pipeline_default in enabled:
        return pipeline_default, ChosenBy.pipeline_default.value
    return None


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
) -> PipelineRun:
    """Resolve and insert a queued run (see the module docstring). Does not commit."""
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
    present = set(inputs)
    seed = secrets.randbelow(2**32) if seed is None else seed
    settings = _settings(db, pipeline.id)
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
    db.add(run)
    for role, input_file in inputs.items():
        db.add(RunInput(pipeline_run_id=run.id, role=role, input_file_id=input_file.id))

    # Which slots will run, in dependency order (pipeline.md 6 items 1-4). A port
    # is "available" if some selected upstream slot may produce it.
    selected: dict[str, Mapping[str, Any]] = {}
    rows: dict[str, RunSlot] = {}
    positions = {s["name"]: i for i, s in enumerate(pipeline.slots)}
    for name in pipeline.topological_order:
        slot = pipeline.slot(name)
        row = RunSlot(
            id=uuid.uuid4(),
            pipeline_run_id=run.id,
            slot=name,
            position=positions[name],
            optional=bool(slot.get("optional", False)),
        )
        rows[name] = row
        if not condition_holds(slot.get("when"), present):
            row.status, row.reason = (
                SlotStatus.skipped.value,
                SlotReason.condition_false.value,
            )
            continue
        if not _required_ports_available(slot, present, selected):
            if not row.optional:
                raise RunRefused(
                    "slot_unsatisfiable",
                    f"slot '{name}' cannot run: an upstream slot is not available",
                    slot=name,
                )
            row.status, row.reason = (
                SlotStatus.skipped.value,
                SlotReason.upstream_absent.value,
            )
            continue
        choice = choose_implementation(
            slot, settings.get(name, {}), requested.get(name)
        )
        if choice is None:
            if not row.optional:
                raise RunRefused(
                    "no_implementation",
                    f"slot '{name}' has no enabled implementation",
                    slot=name,
                )
            row.status, row.reason = (
                SlotStatus.unavailable.value,
                SlotReason.no_implementation.value,
            )
            continue
        impl_id, chosen_by = choice
        impl = pipeline.implementation(name, impl_id)
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
            check_implementation(pipeline, name, impl_id, manifest)
        except PipelineError as exc:
            raise RunRefused("implementation_invalid", str(exc), slot=name) from exc
        row.status = SlotStatus.selected.value
        row.implementation_id = impl_id
        row.step_name = manifest["name"]
        row.step_version = manifest["version"]
        row.image = impl["image"]
        row.stub = bool(manifest.get("stub", False))
        row.chosen_by = chosen_by
        row.effective_parameters = effective_parameters(manifest, impl, case, seed)
        _resolve_bundles(db, row, manifest, impl, bundles)
        selected[name] = slot

    for name in sorted(rows, key=positions.get):
        db.add(rows[name])
    db.flush()
    return run


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
