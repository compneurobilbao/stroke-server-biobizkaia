"""Build a ``RunProvenance`` record from the database (docs/data-model.md section 8).

Used to stage the ``run.provenance`` source (pipeline.md 3.1) for the report
step, with ``status: running``. Writing the run-level file next to the
derivatives is card F0-17's job; it can use the same function.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.case import InputFile
from app.models.pipeline_run import PipelineRun, RunInput, RunSlot, StepRun
from app.models.reference_bundle import ReferenceBundle
from contracts.validate import CONTRACT_VERSION

RUN_STATUS = {"queued": "running", "running": "running", "awaiting_review": "running"}
TERMINAL_STEP = ("succeeded", "failed", "canceled")


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


def build_run_provenance(db: Session, run: PipelineRun) -> dict[str, Any]:
    _version, types_version = run.platform_version, run.artefact_types_version
    record: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
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
            "artefact_types_version": types_version,
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
    record["accounting"] = {
        "submitted_by": run.accounting_submitted_by,
        "submit_as": run.accounting_submit_as,
        "billing_account": run.accounting_billing_account,
    }
    slots = db.scalars(
        select(RunSlot)
        .where(RunSlot.pipeline_run_id == run.id)
        .order_by(RunSlot.position)
    ).all()
    record["slots"] = []
    for slot in slots:
        if slot.status != "selected":
            record["slots"].append(
                {"slot": slot.slot, "status": slot.status, "reason": slot.reason}
            )
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
        record["slots"].append(
            {
                "slot": slot.slot,
                "status": "selected",
                "implementation": {
                    "id": slot.implementation_id,
                    "step": {"name": slot.step_name, "version": slot.step_version},
                    "image": slot.image,
                    "stub": slot.stub,
                    "chosen_by": slot.chosen_by,
                },
                "bundles": sorted(bundles, key=lambda b: b["name"]),
            }
        )
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
