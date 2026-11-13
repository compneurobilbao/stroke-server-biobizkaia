"""The pipeline orchestrator: drives one ``PipelineRun`` to a final state (card F0-15).

Design (docs/processing-pipeline.md, "Pipeline orchestrator"):

- **Re-derive, never remember.** Each ``tick`` reads the run, its slots and its
  attempts from the database and decides the next action from those rows only.
  Nothing is kept in memory between ticks, so a restarted worker (or the
  recovery task) continues any run from where it is.
- **One driver per run (lease).** ``pipeline_runs.orchestrator_task_id`` names
  the Celery task driving the run and ``updated_at`` is its heartbeat. A task
  claims the run with a compare-and-set that succeeds only if the run is
  unclaimed, already its own, or its heartbeat is older than the lease; a task
  that loses the lease stops.
- **Compare-and-set transitions** (docs/data-model.md section 7): every status
  change is ``UPDATE ... WHERE id = :id AND status IN (:allowed)``.
- **Attempts.** A selected slot gets an attempt (``step_runs`` row, ``queued``)
  when every consumed port it stages is available; the attempt is submitted to
  its executor, the persisted handle is stored in ``executor_state``, then it is
  polled until finished, collected, checked against the contract
  (``orchestrator.collection``), its outputs are copied to the case store and
  recorded as artefacts, and the executor's staging is cleaned up.
- **Failure** stops the run: no new attempt starts, live attempts finish, then
  the run is ``failed``. ``failed`` and ``timed_out`` attempts are retried
  automatically (``trigger = auto_retry``) up to ``max_attempts`` attempts per
  start or resume; ``rejected`` and ``contract_violation`` never are.
- **Cancellation** uses the run's ``cancel_requested_at`` flag (set by the API):
  queued attempts are canceled, running ones get ``executor.cancel`` and are
  collected when the executor reports them finished; then the run is
  ``canceled``. An attempt that succeeds meanwhile keeps its result.
- **Resume** is an API transition ``failed|canceled -> queued``. When the
  orchestrator next moves the run ``queued -> running`` it creates a
  ``user_resume`` attempt for every selected slot whose latest attempt is failed
  or canceled; slots that succeeded are kept.

Legacy runs (``pipeline_id = 'legacy'``) are not driven here; see
``orchestrator.legacy``.
"""

from __future__ import annotations

import enum
import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.models.artifact import Artifact, ArtifactOrigin, Report
from app.models.case import Case, InputFile
from app.services import bids
from app.models.pipeline_run import (
    LEGACY_PIPELINE_ID,
    PipelineRun,
    RunInput,
    RunSlot,
    StepRun,
)
from app.models.reference_bundle import ReferenceBundle
from orchestrator import events
from orchestrator.bundle_resolver import BundleError, BundleResolver, to_mount
from orchestrator.catalog import PipelineRegistry, StepCatalog
from orchestrator.collection import Verdict, assess
from orchestrator.executors.base import (
    Accounting,
    BundleMount,
    Executor,
    ExecutorError,
    ExecutorHandle,
    ExecutorRefused,
    JobState,
    Resources,
    StepInvocation,
)
from orchestrator.pipeline import (
    PipelineDefinition,
    artefact_types,
    sources_of,
    staged_name,
)
from orchestrator.provenance import build_run_provenance, timestamp
from orchestrator.store import RunStore, sha256_file

log = logging.getLogger(__name__)

LIVE = ("queued", "running")
RUN_HEADER_FIELDS = (
    "pipeline",
    "platform",
    "created_at",
    "seed",
    "input_set",
    "inputs",
    "slots",
    "accounting",
)
STOPPED = ("failed", "canceled")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class TickResult(str, enum.Enum):
    CONTINUE = "continue"  # call tick again after the poll interval
    FINISHED = "finished"  # the run is final (or not drivable by this orchestrator)
    LOST = "lost"  # another task holds the lease


@dataclass(frozen=True)
class OrchestratorConfig:
    poll_seconds: float = 5.0
    max_attempts: int = 2  # per slot, per start or resume (docs/data-model.md 7.2)
    lease_seconds: int = 600
    max_consecutive_errors: int = 20


class ExecutorRegistry:
    """Executors available to this worker and which one runs each step.

    The choice is deployment configuration (D3): ``default`` for every step,
    ``per_step`` to override by step name.
    """

    def __init__(
        self,
        executors: Mapping[str, Executor],
        default: str,
        per_step: Mapping[str, str] | None = None,
    ) -> None:
        if default not in executors:
            raise ValueError(f"default executor {default!r} is not configured")
        self._executors = dict(executors)
        self.default = default
        self.per_step = dict(per_step or {})

    def for_step(self, step_name: str) -> Executor:
        return self.get(self.per_step.get(step_name, self.default))

    def get(self, name: str) -> Executor:
        try:
            return self._executors[name]
        except KeyError as exc:
            raise ExecutorError(
                f"executor {name!r} is not configured on this worker"
            ) from exc


@dataclass
class _Ctx:
    db: Session
    run: PipelineRun
    pipeline: PipelineDefinition
    owner: str
    slots: dict[str, RunSlot] = field(default_factory=dict)
    attempts: dict[uuid.UUID, list[StepRun]] = field(default_factory=dict)
    inputs: dict[str, InputFile] = field(default_factory=dict)

    def latest(self, slot: RunSlot) -> StepRun | None:
        attempts = self.attempts.get(slot.id, [])
        return attempts[-1] if attempts else None

    def succeeded(self, slot: RunSlot) -> StepRun | None:
        return next(
            (a for a in self.attempts.get(slot.id, []) if a.status == "succeeded"), None
        )

    @property
    def selected(self) -> list[RunSlot]:
        return [s for s in self.slots.values() if s.status == "selected"]


class Orchestrator:
    def __init__(
        self,
        session_factory: Callable[[], Session],
        *,
        pipelines: PipelineRegistry,
        steps: StepCatalog,
        executors: ExecutorRegistry,
        bundles: BundleResolver,
        store: RunStore,
        settings: Any = None,
        config: OrchestratorConfig | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.session_factory = session_factory
        self.pipelines = pipelines
        self.steps = steps
        self.executors = executors
        self.bundles = bundles
        self.store = store
        self.settings = settings
        self.config = config or OrchestratorConfig()
        self.clock = clock

    # ------------------------------------------------------------------ driving

    def drive(
        self,
        run_id: uuid.UUID,
        owner: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> TickResult:
        """Tick until the run is final or the lease is lost. Transient errors
        (executor unreachable, database hiccup) are retried; after
        ``max_consecutive_errors`` the task gives up and the recovery task takes
        the run over once the lease expires."""
        errors = 0
        while True:
            try:
                result = self.tick(run_id, owner)
                errors = 0
            except Exception:
                errors += 1
                log.exception(
                    "Orchestrator tick failed for run %s (%d)", run_id, errors
                )
                if errors >= self.config.max_consecutive_errors:
                    return TickResult.LOST
                result = TickResult.CONTINUE
            if result is not TickResult.CONTINUE:
                return result
            sleep(self.config.poll_seconds)

    def tick(self, run_id: uuid.UUID, owner: str) -> TickResult:
        with self.session_factory() as db:
            if not self._claim(db, run_id, owner):
                db.rollback()
                return TickResult.LOST
            db.commit()
            run = db.get(PipelineRun, run_id)
            if (
                run is None
                or run.pipeline_id == LEGACY_PIPELINE_ID
                or run.status not in ("queued", "running")
            ):
                return TickResult.FINISHED
            pipeline = self.pipelines.get(run.pipeline_id, run.pipeline_version)
            if pipeline is None or pipeline.sha256 != run.pipeline_sha256:
                return self._pipeline_unavailable(db, run)
            ctx = self._context(db, run, pipeline, owner)
            if run.status == "queued":
                if not self._start(ctx):
                    return TickResult.CONTINUE
                ctx = self._context(db, run, pipeline, owner)
            for attempt in [
                a for s in ctx.selected if (a := ctx.latest(s)) and a.status in LIVE
            ]:
                self._advance(ctx, attempt)
            return self._decide(self._context(db, run, pipeline, owner))

    def _claim(self, db: Session, run_id: uuid.UUID, owner: str) -> bool:
        now = self.clock()
        stale = now - timedelta(seconds=self.config.lease_seconds)
        result = db.execute(
            update(PipelineRun)
            .where(
                PipelineRun.id == run_id,
                or_(
                    PipelineRun.orchestrator_task_id.is_(None),
                    PipelineRun.orchestrator_task_id == owner,
                    PipelineRun.updated_at < stale,
                ),
            )
            .values(orchestrator_task_id=owner, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def _context(self, db, run, pipeline, owner) -> _Ctx:
        db.expire_all()
        run = db.get(PipelineRun, run.id)
        ctx = _Ctx(db=db, run=run, pipeline=pipeline, owner=owner)
        for slot in db.scalars(
            select(RunSlot).where(RunSlot.pipeline_run_id == run.id)
        ):
            ctx.slots[slot.slot] = slot
        for attempt in db.scalars(
            select(StepRun)
            .where(StepRun.pipeline_run_id == run.id)
            .order_by(StepRun.attempt)
        ):
            ctx.attempts.setdefault(attempt.run_slot_id, []).append(attempt)
        for role, input_file in db.execute(
            select(RunInput.role, InputFile)
            .join(InputFile, InputFile.id == RunInput.input_file_id)
            .where(RunInput.pipeline_run_id == run.id)
        ):
            ctx.inputs[role] = input_file
        return ctx

    # ---------------------------------------------------------- run transitions

    def _cas_run(
        self, db: Session, run: PipelineRun, allowed: tuple[str, ...], **values
    ) -> bool:
        result = db.execute(
            update(PipelineRun)
            .where(PipelineRun.id == run.id, PipelineRun.status.in_(allowed))
            .values(updated_at=self.clock(), **values)
            .execution_options(synchronize_session=False)
        )
        db.flush()
        return result.rowcount == 1

    def _cas_step(
        self, db: Session, step: StepRun, allowed: tuple[str, ...], **values
    ) -> bool:
        result = db.execute(
            update(StepRun)
            .where(StepRun.id == step.id, StepRun.status.in_(allowed))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def _start(self, ctx: _Ctx) -> bool:
        """``queued -> running``; on resume, new attempts for failed or canceled slots."""
        run, db = ctx.run, ctx.db
        if not self._cas_run(
            db,
            run,
            ("queued",),
            status="running",
            started_at=run.started_at or self.clock(),
        ):
            db.rollback()
            return False
        for slot in ctx.selected:
            latest = ctx.latest(slot)
            if (
                latest is not None
                and latest.status in STOPPED
                and ctx.succeeded(slot) is None
            ):
                self._new_attempt(ctx, slot, "user_resume")
        prov = self.store.storage.run_provenance_path(run.case_id, run.id)
        if not prov.exists():
            record = build_run_provenance(db, run)
            header = {k: v for k, v in record.items() if k in RUN_HEADER_FIELDS}
            bids.init_run_provenance(self.store.storage, run.case_id, run.id, header)
        else:
            bids.update_run_provenance(
                self.store.storage, run.case_id, run.id, status="running"
            )
        self._cas_run(db, run, ("running",), provenance_path=self.store.relative(prov))
        db.commit()
        return True

    def _finish_run(self, ctx: _Ctx, status: str, **values) -> TickResult:
        db, run = ctx.db, ctx.run
        if not self._cas_run(
            db, run, ("running",), status=status, finished_at=self.clock(), **values
        ):
            db.rollback()
            return TickResult.CONTINUE
        db.refresh(run)
        events.audit(
            db, f"run_{status}", run, {"failure_code": values.get("failure_code")}
        )
        db.commit()
        self._close_provenance(run)
        if status in ("succeeded", "failed"):
            self._notify(db, run)
        return TickResult.FINISHED

    def _close_provenance(self, run: PipelineRun) -> None:
        """Final status in the run-level RunProvenance file (F0-17 case store)."""
        if not self.store.storage.run_provenance_path(run.case_id, run.id).exists():
            return
        try:
            bids.update_run_provenance(
                self.store.storage,
                run.case_id,
                run.id,
                status=run.status,
                finished_at=timestamp(run.finished_at),
            )
        except Exception:
            log.exception("Could not update the provenance file of run %s", run.id)

    def _notify(self, db: Session, run: PipelineRun) -> None:
        try:
            settings = self.settings
            if settings is None:
                from app.core.config import get_settings

                settings = get_settings()
            events.notify_run_finished(db, run, settings)
            db.commit()
        except Exception:
            db.rollback()
            log.exception("Notification dispatch failed for run %s", run.id)

    def _pipeline_unavailable(self, db: Session, run: PipelineRun) -> TickResult:
        live = db.scalar(
            select(StepRun.id).where(
                StepRun.pipeline_run_id == run.id, StepRun.status.in_(LIVE)
            )
        )
        if live is not None:
            # Live attempts cannot be finalised without their slot definition: leave
            # the run to an operator (or to a worker that has the pipeline).
            log.error(
                "Pipeline %s %s unavailable for run %s",
                run.pipeline_id,
                run.pipeline_version,
                run.id,
            )
            return TickResult.LOST
        ok = self._cas_run(
            db,
            run,
            ("queued", "running"),
            status="failed",
            finished_at=self.clock(),
            started_at=run.started_at or self.clock(),
            failure_code="pipeline_unavailable",
            failure_message="The pipeline definition of this run is not available on this deployment.",
        )
        if ok:
            db.refresh(run)
            events.audit(
                db, "run_failed", run, {"failure_code": "pipeline_unavailable"}
            )
            db.commit()
            self._notify(db, run)
        return TickResult.FINISHED

    # ------------------------------------------------------------------ decide

    def _decide(self, ctx: _Ctx) -> TickResult:
        run = ctx.run
        live = [a for s in ctx.selected if (a := ctx.latest(s)) and a.status in LIVE]
        if run.cancel_requested_at is not None:
            if live:
                return TickResult.CONTINUE
            return self._finish_run(ctx, "canceled")
        stopped = [
            a for s in ctx.selected if (a := ctx.latest(s)) and a.status in STOPPED
        ]
        if stopped:
            if live:
                return TickResult.CONTINUE
            first = min(stopped, key=lambda a: (a.finished_at or a.queued_at))
            slot = next(s for s in ctx.selected if s.id == first.run_slot_id)
            code = {"rejected": "input_rejected", "canceled": "step_canceled"}.get(
                first.outcome, "step_failed"
            )
            detail = (first.step_error or {}).get("message") or first.failure_message
            return self._finish_run(
                ctx,
                "failed",
                failure_code=code,
                failure_message=f"Step '{slot.slot}' did not succeed. {detail or ''}".strip()[
                    :1000
                ],
                failed_step_run_id=first.id,
            )
        if all(ctx.succeeded(s) for s in ctx.selected):
            return self._finish_run(ctx, "succeeded")
        created = []
        for name in ctx.pipeline.topological_order:
            slot = ctx.slots.get(name)
            if (
                slot is None
                or slot.status != "selected"
                or ctx.latest(slot) is not None
            ):
                continue
            if self._ready(ctx, slot):
                created.append(self._new_attempt(ctx, slot, "initial"))
        if created:
            ctx.db.commit()
            for attempt in created:
                self._advance(ctx, attempt)
        return TickResult.CONTINUE

    # ----------------------------------------------------------------- attempts

    def _manifest(self, slot: RunSlot) -> Mapping[str, Any] | None:
        return self.steps.manifest(slot.step_name, slot.step_version, slot.image)

    @staticmethod
    def _resources(manifest: Mapping[str, Any] | None) -> Resources:
        if manifest is None:
            return Resources(cpus=1, memory_mb=1, wall_time_minutes=1)
        res = manifest["resources"]
        gpu = res.get("gpu") or {}
        return Resources(
            cpus=int(res["cpus"]),
            memory_mb=int(res["memory_mb"]),
            wall_time_minutes=int(res["wall_time_minutes"]),
            gpus=int(gpu.get("count", 1)) if gpu.get("mode") == "required" else 0,
            scratch_mb=int(res.get("scratch_mb", 1024)),
        )

    def _new_attempt(self, ctx: _Ctx, slot: RunSlot, trigger: str) -> StepRun:
        executor = self.executors.for_step(slot.step_name)
        resources = self._resources(self._manifest(slot))
        previous = ctx.attempts.get(slot.id, [])
        attempt = StepRun(
            id=uuid.uuid4(),
            pipeline_run_id=ctx.run.id,
            run_slot_id=slot.id,
            attempt=(previous[-1].attempt + 1) if previous else 1,
            trigger=trigger,
            status="queued",
            executor=executor.name,
            runtime=executor.runtime,
            image=slot.image,
            queued_at=self.clock(),
            granted_cpus=resources.cpus,
            granted_memory_mb=resources.memory_mb,
            granted_gpus=resources.gpus,
            granted_wall_time_minutes=resources.wall_time_minutes,
            orchestrator_task_id=ctx.owner,
        )
        ctx.db.add(attempt)
        ctx.db.flush()
        ctx.attempts.setdefault(slot.id, []).append(attempt)
        return attempt

    def _fail_queued(
        self, ctx: _Ctx, attempt: StepRun, code: str, message: str
    ) -> None:
        self._cas_step(
            ctx.db,
            attempt,
            ("queued",),
            status="failed",
            outcome="failed",
            finished_at=self.clock(),
            failure_code=code,
            failure_message=message,
        )
        ctx.db.commit()

    def _advance(self, ctx: _Ctx, attempt: StepRun) -> None:
        db, run = ctx.db, ctx.run
        db.refresh(attempt)
        if attempt.status not in LIVE:
            return
        state = attempt.executor_state or {}
        if "handle" not in state:
            if run.cancel_requested_at is not None:
                self._cas_step(
                    db,
                    attempt,
                    ("queued",),
                    status="canceled",
                    outcome="canceled",
                    finished_at=self.clock(),
                    cancel_requested_at=run.cancel_requested_at,
                )
                db.commit()
            else:
                self._submit(ctx, attempt)
            return
        handle = ExecutorHandle.from_dict(state["handle"])
        executor = self.executors.get(handle.executor)
        if run.cancel_requested_at is not None and attempt.cancel_requested_at is None:
            self._cas_step(db, attempt, LIVE, cancel_requested_at=self.clock())
            db.commit()
            executor.cancel(handle)
        status = executor.poll(handle)
        if not status.finished:
            if attempt.status == "queued" and status.state is JobState.RUNNING:
                self._cas_step(
                    db,
                    attempt,
                    ("queued",),
                    status="running",
                    started_at=parse_timestamp(status.started_at) or self.clock(),
                )
                db.commit()
            return
        self._finalize(ctx, attempt, executor, handle)

    def _ready(self, ctx: _Ctx, slot: RunSlot) -> bool:
        manifest = self._manifest(slot)
        if manifest is None:
            return True  # the attempt will fail with manifest_unavailable
        declared = {i["name"] for i in manifest["inputs"]}
        slot_def = ctx.pipeline.slot(slot.slot)
        return all(
            self._resolve_port(ctx, c)[0] != "pending"
            for c in slot_def["consumes"]
            if c["port"] in declared
        )

    def _resolve_port(self, ctx: _Ctx, consume: Mapping[str, Any]) -> tuple[str, Any]:
        """('ready', source) | ('absent', None) | ('pending', None): first available source."""
        for src in sources_of(consume):
            if src.kind == "input":
                if src.name in ctx.inputs:
                    return "ready", self.store.absolute(
                        ctx.inputs[src.name].storage_path
                    )
                continue
            if src.kind == "run":
                return "ready", "run.provenance"
            upstream = ctx.slots.get(src.name)
            if upstream is None or upstream.status != "selected":
                continue
            done = ctx.succeeded(upstream)
            if done is None:
                return "pending", None
            artifact = ctx.db.scalar(
                select(Artifact).where(
                    Artifact.step_run_id == done.id, Artifact.port == src.port
                )
            )
            if artifact is not None:
                return "ready", self.store.absolute(artifact.storage_path)
        return "absent", None

    def _submit(self, ctx: _Ctx, attempt: StepRun) -> None:
        db, run = ctx.db, ctx.run
        slot = next(s for s in ctx.slots.values() if s.id == attempt.run_slot_id)
        manifest = self._manifest(slot)
        if manifest is None:
            return self._fail_queued(
                ctx,
                attempt,
                "manifest_unavailable",
                "The step manifest is not available.",
            )
        slot_def = ctx.pipeline.slot(slot.slot)
        consumes = {c["port"]: c for c in slot_def["consumes"]}

        staged: dict[str, Path] = {}
        staged_meta: dict[str, list] = {}
        for port in manifest["inputs"]:
            consume = consumes.get(port["name"])
            if consume is None:
                continue
            state, source = self._resolve_port(ctx, consume)
            if state != "ready":
                if port.get("required", True) and not consume.get("optional", False):
                    return self._fail_queued(
                        ctx,
                        attempt,
                        "input_unavailable",
                        f"Input '{port['name']}' is not available.",
                    )
                continue
            if source == "run.provenance":
                source = (
                    self.store.staging_dir(run.case_id, run.id, attempt.id)
                    / "run_provenance.json"
                )
                source.parent.mkdir(parents=True, exist_ok=True)
                prov = self.store.storage.run_provenance_path(run.case_id, run.id)
                record = (
                    bids.load_run_provenance(self.store.storage, run.case_id, run.id)
                    if prov.exists()
                    else build_run_provenance(db, run)
                )
                source.write_text(json.dumps(record, indent=2), encoding="utf-8")
            name = staged_name(port["name"], consume["type"])
            staged[name] = source
            staged_meta[port["name"]] = [
                consume["type"],
                sha256_file(source),
                source.stat().st_size,
            ]

        try:
            mounts = self._mounts(db, slot, manifest)
        except BundleError as exc:
            return self._fail_queued(ctx, attempt, "bundle_unavailable", str(exc)[:500])

        work_dir = self.store.work_dir(run.case_id, run.id, slot.slot, attempt.attempt)
        if work_dir.exists() and any(work_dir.iterdir()):
            # A previous driver crashed between submit and storing the handle.
            return self._fail_queued(
                ctx,
                attempt,
                "lost_submission",
                "The attempt was interrupted while being submitted.",
            )
        invocation = StepInvocation(
            step_run_id=str(attempt.id),
            step_name=slot.step_name,
            step_version=slot.step_version,
            resources=Resources(
                cpus=attempt.granted_cpus,
                memory_mb=attempt.granted_memory_mb,
                wall_time_minutes=attempt.granted_wall_time_minutes,
                gpus=attempt.granted_gpus,
                scratch_mb=int(manifest["resources"].get("scratch_mb", 1024)),
            ),
            accounting=Accounting.from_dict(
                {
                    "submitted_by": run.accounting_submitted_by,
                    "submit_as": run.accounting_submit_as,
                    "billing_account": run.accounting_billing_account,
                }
            ),
            work_dir=work_dir,
            image=slot.image,
            inputs=staged,
            params=dict(slot.effective_parameters or {}),
            bundles=mounts,
            attempt=attempt.attempt,
        )
        try:
            executor = self.executors.get(attempt.executor)
            handle = executor.submit(invocation)
        except (ExecutorRefused, NotImplementedError) as exc:
            return self._fail_queued(ctx, attempt, "executor_refused", str(exc)[:500])
        except ExecutorError as exc:
            log.warning("Submission of attempt %s failed: %s", attempt.id, exc)
            return self._fail_queued(
                ctx, attempt, "executor_error", "The executor could not start the step."
            )
        self._cas_step(
            db,
            attempt,
            ("queued",),
            external_job_id=handle.external_job_id[:128],
            executor_state={
                "handle": handle.to_dict(),
                "staged_inputs": staged_meta,
                "bundles": [
                    {
                        "name": m.name,
                        "id": m.id,
                        "version": m.version,
                        "content_digest": m.content_digest,
                    }
                    for m in mounts
                ],
            },
        )
        db.commit()

    def _mounts(
        self, db: Session, slot: RunSlot, manifest: Mapping[str, Any]
    ) -> list[BundleMount]:
        """Re-resolve exactly the bundles recorded at run creation (pinned)."""
        if not slot.bundles:
            return []
        pins = {}
        for link in slot.bundles:
            bundle = db.get(ReferenceBundle, link.reference_bundle_id)
            pins[link.requirement_name] = (bundle.bundle_id, bundle.version)
        requirements = [b for b in manifest.get("bundles", []) if b["name"] in pins]
        resolved = self.bundles.resolve(requirements, pins)
        missing = set(pins) - set(resolved)
        if missing:
            raise BundleError(f"recorded bundles not resolved: {sorted(missing)}")
        return [to_mount(resolved[name]) for name in sorted(resolved)]

    def _finalize(
        self, ctx: _Ctx, attempt: StepRun, executor: Executor, handle: ExecutorHandle
    ) -> None:
        db = ctx.db
        slot = next(s for s in ctx.slots.values() if s.id == attempt.run_slot_id)
        result = executor.collect(handle)
        manifest = self._manifest(slot)
        state = attempt.executor_state or {}
        if manifest is None:
            verdict = Verdict(
                "failed", "manifest_unavailable", "The step manifest is not available."
            )
        else:
            verdict = assess(
                result,
                manifest=manifest,
                slot=ctx.pipeline.slot(slot.slot),
                present_inputs=set(ctx.inputs),
                staged_inputs={
                    k: tuple(v) for k, v in state.get("staged_inputs", {}).items()
                },
                mounted=[
                    BundleMount(host_path=Path("/"), **b)
                    for b in state.get("bundles", [])
                ],
            )
        execution = result.execution
        now = self.clock()
        started = attempt.started_at or parse_timestamp(execution.get("started_at"))
        if attempt.status == "queued" and verdict.status != "canceled":
            self._cas_step(
                db, attempt, ("queued",), status="running", started_at=started or now
            )

        verdict, artifacts = self._store_step(
            ctx, attempt, slot, manifest, verdict, result
        )

        usage = result.usage
        values = dict(
            status=verdict.status,
            outcome=verdict.outcome,
            finished_at=parse_timestamp(execution.get("finished_at")) or now,
            exit_code=result.exit_code,
            usage_wall_seconds=_dec(usage.wall_seconds),
            usage_cpu_core_hours=_dec(usage.cpu_core_hours),
            usage_gpu_hours=_dec(usage.gpu_hours),
            usage_max_rss_mb=int(usage.max_rss_mb)
            if usage.max_rss_mb is not None
            else None,
            step_provenance=verdict.provenance,
            step_provenance_sha256=verdict.provenance_sha256,
            step_error=verdict.error,
            failure_code=verdict.failure_code,
            failure_message=verdict.failure_message,
            log_path=self.store.relative(result.log_path),
        )
        if attempt.started_at is None and started is not None:
            values["started_at"] = started
        if not self._cas_step(db, attempt, LIVE, **values):
            db.rollback()
            return
        for artifact in artifacts:
            db.add(artifact)
        db.flush()
        self._create_report(ctx, attempt, slot, artifacts)
        if self._should_retry(ctx, slot, attempt, verdict):
            self._new_attempt(ctx, slot, "auto_retry")
        db.commit()
        try:
            executor.cleanup(handle)
        except Exception:
            log.exception("Cleanup of attempt %s failed", attempt.id)

    def _should_retry(
        self, ctx: _Ctx, slot: RunSlot, attempt: StepRun, verdict: Verdict
    ) -> bool:
        if (
            verdict.outcome not in ("failed", "timed_out")
            or ctx.run.cancel_requested_at is not None
        ):
            return False
        if verdict.failure_code == "manifest_unavailable":
            return False
        # Attempts since the last start or resume: trailing auto retries plus the
        # attempt that started the streak.
        streak = 0
        for previous in reversed(ctx.attempts.get(slot.id, [])):
            streak += 1
            if previous.trigger != "auto_retry":
                break
        return streak < self.config.max_attempts

    def _store_step(self, ctx, attempt, slot, manifest, verdict, result):
        """Store the attempt in the case store (F0-17) and build its artefact rows.

        Outputs only for a succeeded attempt; every attempt is merged into the
        run-level RunProvenance. A store failure downgrades success to
        ``contract_violation``.
        """
        run = ctx.run
        execution = {**result.execution, "outcome": verdict.outcome}
        case = bids.CaseIdentity.from_case(ctx.db.get(Case, run.case_id))
        _version, types = artefact_types()
        declared = {o["name"]: o["type"] for o in (manifest or {}).get("outputs", [])}
        prov_path = result.out_dir / "provenance.json"
        kwargs = dict(
            slot=slot.slot,
            step_name=slot.step_name,
            step_version=slot.step_version,
            error=verdict.error,
            image=slot.image,
        )
        try:
            outputs = [
                bids.StepOutput(
                    port, bids.BidsNaming.from_type(types[declared[port]]), source
                )
                for port, source in sorted(verdict.outputs.items())
            ]
            stored = bids.store_step_result(
                self.store.storage,
                case,
                run.id,
                execution=execution,
                outputs=outputs if verdict.status == "succeeded" else (),
                provenance_path=prov_path if verdict.provenance is not None else None,
                **kwargs,
            )
        except (ValueError, OSError, KeyError) as exc:
            log.warning("Storing attempt %s failed: %s", attempt.id, exc)
            verdict = Verdict(
                "contract_violation",
                "contract_violation_store",
                "The step results could not be stored in the case store.",
            )
            try:
                bids.store_step_result(
                    self.store.storage,
                    case,
                    run.id,
                    execution={**execution, "outcome": verdict.outcome},
                    **{**kwargs, "error": None},
                )
            except Exception:
                log.exception(
                    "Could not record attempt %s in the run provenance", attempt.id
                )
            return verdict, []
        if verdict.status != "succeeded":
            return verdict, []
        synthetic = bool(slot.stub) or any(
            ctx.db.get(ReferenceBundle, link.reference_bundle_id).synthetic
            for link in slot.bundles
        )
        artifacts = [
            Artifact(
                id=uuid.uuid4(),
                case_id=run.case_id,
                pipeline_run_id=run.id,
                step_run_id=attempt.id,
                origin=ArtifactOrigin.step.value,
                port=port,
                artefact_type=declared[port],
                storage_path=item.relative_path,
                sha256=item.sha256,
                size_bytes=item.size_bytes,
                synthetic=synthetic,
            )
            for port, item in sorted(stored.outputs.items())
        ]
        return verdict, artifacts

    def _create_report(self, ctx, attempt, slot, artifacts: list[Artifact]) -> None:
        """A ``reports`` row when an attempt produced both ``report_pdf`` and ``report_json``."""
        by_type = {a.artefact_type: a for a in artifacts}
        pdf, doc = by_type.get("report_pdf"), by_type.get("report_json")
        if pdf is None or doc is None:
            return
        params = slot.effective_parameters or {}
        template = params.get("template")
        if not isinstance(template, str) or not template:
            template = slot.implementation_id
        language = (
            params.get("language") if isinstance(params.get("language"), str) else "en"
        )
        ctx.db.add(
            Report(
                case_id=ctx.run.case_id,
                pipeline_run_id=ctx.run.id,
                step_run_id=attempt.id,
                pdf_artifact_id=pdf.id,
                json_artifact_id=doc.id,
                template=template,
                language=language[:8],
                content_sha256=doc.sha256,
            )
        )


def _dec(value: float | None) -> Decimal | None:
    return None if value is None else Decimal(str(round(value, 4)))


def stale_runs(
    db: Session, lease_seconds: int, now: datetime | None = None
) -> list[uuid.UUID]:
    """Active non-legacy runs whose driver stopped heartbeating (crash recovery, 7.1)."""
    now = now or utcnow()
    stale = now - timedelta(seconds=lease_seconds)
    return list(
        db.scalars(
            select(PipelineRun.id).where(
                PipelineRun.status.in_(("queued", "running")),
                PipelineRun.pipeline_id != LEGACY_PIPELINE_ID,
                PipelineRun.updated_at < stale,
            )
        )
    )
