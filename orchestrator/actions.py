"""Run actions requested by users: cancel and resume (docs/data-model.md section 7.3).

The run API (card F0-30) calls these inside its request transaction, commits,
and then performs the side effect named in the result: revoke the queued Celery
task (``revoke_task``) or enqueue ``worker.tasks.run_pipeline`` (``enqueue``)
and store its id in ``pipeline_runs.orchestrator_task_id``.

Every transition is a compare-and-set; repeated calls are idempotent as the
tables of section 7.3 specify. Refusals raise ``ActionRefused`` with the code the
API returns with HTTP 409.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.pipeline_run import (
    ACTIVE_STATUSES,
    LEGACY_PIPELINE_ID,
    PipelineRun,
    RunSlot,
    StepRun,
)
from app.models.reference_bundle import ReferenceBundle, SlotImplementationSetting
from orchestrator import events
from orchestrator.bundle_resolver import BundleError, BundleResolver
from orchestrator.catalog import PipelineRegistry, StepCatalog


class ActionRefused(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ActionResult:
    status: str  # run status after the action
    cancel_requested: bool = False
    revoke_task: bool = False  # cancel of a queued run: revoke its Celery task
    enqueue: bool = False  # resume: enqueue the orchestrator task
    changed: bool = True  # False for idempotent repeats (no audit event written)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _actor(user_id: uuid.UUID) -> dict:
    return {"actor": f"user:{user_id}", "actor_user_id": user_id}


def _get(db: Session, run_id: uuid.UUID) -> PipelineRun:
    run = db.get(PipelineRun, run_id)
    if run is None:
        raise ActionRefused("run_not_found", "Run not found.")
    if run.pipeline_id == LEGACY_PIPELINE_ID:
        raise ActionRefused(
            "legacy_run", "Legacy runs are managed through the legacy study endpoints."
        )
    return run


def cancel_run(db: Session, run_id: uuid.UUID, user_id: uuid.UUID) -> ActionResult:
    run = _get(db, run_id)
    now = _now()
    if run.status == "queued":
        done = db.execute(
            update(PipelineRun)
            .where(PipelineRun.id == run.id, PipelineRun.status == "queued")
            .values(
                status="canceled",
                finished_at=now,
                cancel_requested_at=now,
                cancel_requested_by_user_id=user_id,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        if done:
            db.execute(
                update(StepRun)
                .where(StepRun.pipeline_run_id == run.id, StepRun.status == "queued")
                .values(
                    status="canceled",
                    outcome="canceled",
                    finished_at=now,
                    cancel_requested_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            db.refresh(run)
            events.audit(db, "run_canceled", run, **_actor(user_id))
            return ActionResult("canceled", True, revoke_task=True)
        db.refresh(run)
    if run.status in ("running", "awaiting_review"):
        done = db.execute(
            update(PipelineRun)
            .where(
                PipelineRun.id == run.id,
                PipelineRun.status.in_(("running", "awaiting_review")),
                PipelineRun.cancel_requested_at.is_(None),
            )
            .values(
                cancel_requested_at=now,
                cancel_requested_by_user_id=user_id,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        ).rowcount
        db.refresh(run)
        if done:
            events.audit(db, "run_cancel_requested", run, **_actor(user_id))
            return ActionResult(run.status, True)
        if run.status in ("running", "awaiting_review"):
            return ActionResult(run.status, True, changed=False)
    if run.status == "canceled":
        return ActionResult("canceled", True, changed=False)
    raise ActionRefused("run_not_active", "The run is not active.")


def resume_run(
    db: Session,
    run_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    pipelines: PipelineRegistry,
    steps: StepCatalog,
    bundles: BundleResolver,
) -> ActionResult:
    run = _get(db, run_id)
    if run.status == "succeeded":
        raise ActionRefused("run_succeeded", "A succeeded run cannot be resumed.")
    if run.status in ACTIVE_STATUSES:
        if run.cancel_requested_at is not None:
            raise ActionRefused("cancel_in_progress", "The run is being canceled.")
        return ActionResult(run.status, changed=False)

    # Preconditions (7.3), in order.
    if run.failed_step_run_id is not None:
        failed = db.get(StepRun, run.failed_step_run_id)
        if failed is not None and failed.outcome == "rejected":
            raise ActionRefused(
                "input_rejected",
                "The input was rejected; resuming gives the same result.",
            )
    pipeline = pipelines.get(run.pipeline_id, run.pipeline_version)
    if pipeline is None or pipeline.sha256 != run.pipeline_sha256:
        raise ActionRefused(
            "pipeline_unavailable", "The pipeline of this run is no longer available."
        )
    settings = {
        (s.slot, s.implementation_id): s
        for s in db.scalars(
            select(SlotImplementationSetting).where(
                SlotImplementationSetting.pipeline_id == run.pipeline_id
            )
        )
    }
    slots = db.scalars(
        select(RunSlot).where(
            RunSlot.pipeline_run_id == run.id, RunSlot.status == "selected"
        )
    ).all()
    for slot in slots:
        succeeded = db.scalar(
            select(StepRun.id).where(
                StepRun.run_slot_id == slot.id, StepRun.status == "succeeded"
            )
        )
        if succeeded is not None:
            continue
        setting = settings.get((slot.slot, slot.implementation_id))
        if setting is None or not setting.enabled:
            raise ActionRefused(
                "implementation_disabled",
                f"Implementation '{slot.implementation_id}' of slot '{slot.slot}' is disabled.",
            )
        if slot.bundles:
            pins = {}
            for link in slot.bundles:
                bundle = db.get(ReferenceBundle, link.reference_bundle_id)
                pins[link.requirement_name] = (bundle.bundle_id, bundle.version)
            manifest = (
                steps.manifest(slot.step_name, slot.step_version, slot.image) or {}
            )
            requirements = [b for b in manifest.get("bundles", []) if b["name"] in pins]
            try:
                resolved = bundles.resolve(requirements, pins)
            except BundleError as exc:
                raise ActionRefused("bundle_unavailable", str(exc)) from exc
            if set(resolved) != set(pins):
                raise ActionRefused(
                    "bundle_unavailable", "A bundle of this run is unavailable."
                )
    other = db.scalar(
        select(PipelineRun.id).where(
            PipelineRun.case_id == run.case_id,
            PipelineRun.id != run.id,
            PipelineRun.status.in_(ACTIVE_STATUSES),
        )
    )
    if other is not None:
        raise ActionRefused("active_run_exists", "Another run of this case is active.")

    now = _now()
    done = db.execute(
        update(PipelineRun)
        .where(PipelineRun.id == run.id, PipelineRun.status.in_(("failed", "canceled")))
        .values(
            status="queued",
            finished_at=None,
            failure_code=None,
            failure_message=None,
            failed_step_run_id=None,
            cancel_requested_at=None,
            cancel_requested_by_user_id=None,
            resume_count=PipelineRun.resume_count + 1,
            orchestrator_task_id=None,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    ).rowcount
    db.refresh(run)
    if not done:
        return ActionResult(run.status, changed=False)
    events.audit(db, "run_resumed", run, **_actor(user_id))
    return ActionResult("queued", enqueue=True)
