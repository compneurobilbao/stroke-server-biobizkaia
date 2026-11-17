"""Build a ``RunProvenance`` record from the database (docs/data-model.md section 8).

Used to start the run-level ``provenance.json`` in the case store (F0-17) and to
stage the ``run.provenance`` source (pipeline.md 3.1) for steps such as the report,
with ``status: running``.

Contract version of the record (card F1-19, data-model.md 8): the record carries the
run plan (``plan`` and the slots' ``plan_role``) only when every applicable slot row
(reason other than ``condition_false`` and ``not_requested``) has a ``plan_role``,
and then declares contract 1.2; so does a record with a slot
``implementation.upstream``. Otherwise (runs created before the plan-aware
orchestrator) it carries neither and declares contract 1.1.

A step reads the record in the form of the contract its manifest declares
(``run_provenance_for_step``): a step declaring 1.0 or 1.1 gets the record without
the 1.2 fields, which it could not validate (step images copy the contract schemas
of their build, presets.md 4.3).
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.case import InputFile
from app.models.pipeline_run import PipelineRun, RunInput, RunSlot, StepRun
from app.models.reference_bundle import ReferenceBundle
from contracts import validate as contract

RUN_STATUS = {"queued": "running", "running": "running", "awaiting_review": "running"}
TERMINAL_STEP = ("succeeded", "failed", "canceled")
# plan, plan_role, not_requested, chosen_by: preset, implementation.upstream
PLAN_CONTRACT_VERSION = "1.2"
BASE_CONTRACT_VERSION = "1.1"  # a record that uses none of them
NO_PLAN_ROLE = ("condition_false", "not_requested")


def timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _execution(step: StepRun) -> dict[str, Any]:
    record: dict[str, Any] = {"executor": step.executor, "runtime": step.runtime}
    if step.image:
        record["image"] = step.image
    if step.sif_sha256:
        record["sif_sha256"] = step.sif_sha256
    if step.external_job_id:
        record["external_job_id"] = step.external_job_id
    record["attempt"] = step.attempt
    for key in ("queued_at", "started_at", "finished_at"):
        value = timestamp(getattr(step, key))
        if value:
            record[key] = value
    record["exit_code"] = step.exit_code
    record["outcome"] = step.outcome
    record["resources"] = {
        "cpus": step.granted_cpus or 1,
        "memory_mb": step.granted_memory_mb or 1,
        "gpus": step.granted_gpus or 0,
        "wall_time_minutes": step.granted_wall_time_minutes or 1,
    }
    usage = {
        "wall_seconds": step.usage_wall_seconds,
        "cpu_core_hours": step.usage_cpu_core_hours,
        "gpu_hours": step.usage_gpu_hours,
        "max_rss_mb": step.usage_max_rss_mb,
    }
    usage = {k: float(v) for k, v in usage.items() if v is not None}
    if usage:
        record["usage"] = usage
    return record


def has_plan(slots: list[RunSlot]) -> bool:
    """The data-model.md 8 rule: every applicable slot row has a ``plan_role``."""
    return all(s.plan_role is not None for s in slots if s.reason not in NO_PLAN_ROLE)


def plan_record(run: PipelineRun) -> dict[str, Any]:
    """``RunProvenance.plan`` (provenance.md 3.4) from ``pipeline_runs``."""
    preset = None
    if run.plan_mode == "preset":
        preset = {
            "id": run.preset_id,
            "version": run.preset_version,
            "sha256": run.preset_sha256,
        }
    return {
        "mode": run.plan_mode,
        "preset": preset,
        "targets": copy.deepcopy(dict(run.targets or {})),
    }


def build_run_provenance(db: Session, run: PipelineRun) -> dict[str, Any]:
    slots = db.scalars(
        select(RunSlot)
        .where(RunSlot.pipeline_run_id == run.id)
        .order_by(RunSlot.position)
    ).all()
    with_plan = has_plan(slots)
    upstream = any(s.upstream is not None for s in slots if s.status == "selected")
    record: dict[str, Any] = {
        "contract_version": (
            PLAN_CONTRACT_VERSION if with_plan or upstream else BASE_CONTRACT_VERSION
        ),
        "kind": "RunProvenance",
        "run_id": str(run.id),
        "case_id": str(run.case_id),
        "pipeline": {
            "id": run.pipeline_id,
            "version": run.pipeline_version,
            "sha256": run.pipeline_sha256,
        },
        "platform": {
            "version": run.platform_version,
            "artefact_types_version": run.artefact_types_version,
        },
        "status": RUN_STATUS.get(run.status, run.status),
        "created_at": timestamp(run.created_at),
        "seed": run.seed,
        "input_set": run.input_set,
    }
    if run.finished_at is not None and record["status"] != "running":
        record["finished_at"] = timestamp(run.finished_at)
    inputs = db.execute(
        select(RunInput.role, InputFile)
        .join(InputFile, InputFile.id == RunInput.input_file_id)
        .where(RunInput.pipeline_run_id == run.id)
        .order_by(RunInput.role)
    ).all()
    record["inputs"] = [
        {
            "name": role,
            "type": f.artefact_type,
            "sha256": f.sha256,
            "size_bytes": f.size_bytes,
        }
        for role, f in inputs
    ]
    if with_plan:
        record["plan"] = plan_record(run)
    record["accounting"] = {
        "submitted_by": run.accounting_submitted_by,
        "submit_as": run.accounting_submit_as,
        "billing_account": run.accounting_billing_account,
    }
    record["slots"] = []
    for slot in slots:
        entry: dict[str, Any] = {"slot": slot.slot, "status": slot.status}
        if with_plan and slot.plan_role is not None:
            entry["plan_role"] = slot.plan_role
        if slot.status != "selected":
            entry["reason"] = slot.reason
            record["slots"].append(entry)
            continue
        bundles = []
        for link in slot.bundles:
            bundle = db.get(ReferenceBundle, link.reference_bundle_id)
            bundles.append(
                {
                    "name": link.requirement_name,
                    "id": bundle.bundle_id,
                    "version": bundle.version,
                    "content_digest": bundle.content_digest,
                }
            )
        implementation = {
            "id": slot.implementation_id,
            "step": {"name": slot.step_name, "version": slot.step_version},
            "image": slot.image,
            "stub": slot.stub,
            "chosen_by": slot.chosen_by,
        }
        if slot.upstream is not None:
            implementation["upstream"] = copy.deepcopy(dict(slot.upstream))
        entry["implementation"] = implementation
        entry["bundles"] = sorted(bundles, key=lambda b: b["name"])
        record["slots"].append(entry)
    names = {s.id: s.slot for s in slots}
    steps = db.scalars(
        select(StepRun)
        .where(StepRun.pipeline_run_id == run.id, StepRun.status.in_(TERMINAL_STEP))
        .order_by(StepRun.queued_at, StepRun.attempt)
    ).all()
    record["steps"] = [
        {
            "slot": names[s.run_slot_id],
            "execution": _execution(s),
            "provenance": s.step_provenance,
            "error": s.step_error,
        }
        for s in steps
    ]
    return record


def run_provenance_for_step(
    record: dict[str, Any], manifest: Mapping[str, Any] | None
) -> dict[str, Any]:
    """The run record as staged for a step that reads ``run.provenance``.

    A step always gets the record in the form of the contract its manifest declares:
    a step declaring the record's version or a later one gets ``record`` unchanged; a
    step declaring an older version implements that contract and rejects newer
    documents (versioning.md 2 rule 4; its image validates the record against the
    schemas of its build), so the record is relabelled to the step's version, without
    ``plan``, ``plan_role``, ``implementation.upstream`` and ``inputs[].origin``
    (1.2 additions; for a 1.1 record nothing but the label changes, since
    RunProvenance has no field added in 1.1). Only the full plan has such a form:
    a preset or free plan, slots outside the plan (``not_requested``) and preset
    choices (``chosen_by: preset``) do not, run creation refuses those plans for
    such steps (``step_contract_too_old``,
    ``orchestrator.planning.provenance_consumer_reasons``), and a record that still
    has them is returned unchanged rather than altered.
    """
    consumer = contract.contract_minor((manifest or {}).get("contract_version"))
    current = contract.contract_minor(record.get("contract_version"))
    if consumer < 0 or consumer >= current:
        return record
    plan = record.get("plan")
    if (plan is not None and plan.get("mode") != "full") or any(
        s.get("reason") == "not_requested"
        or s.get("implementation", {}).get("chosen_by") == "preset"
        for s in record.get("slots", [])
    ):
        return record
    out = copy.deepcopy(record)
    out["contract_version"] = f"{contract.CONTRACT_MAJOR}.{consumer}"
    out.pop("plan", None)
    for item in out.get("inputs", []):
        item.pop("origin", None)
    for slot in out.get("slots", []):
        slot.pop("plan_role", None)
        slot.get("implementation", {}).pop("upstream", None)
    return out
