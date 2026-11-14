"""Pipeline orchestrator (card F0-15): run creation, driving, cancel, resume, retry.

Most tests use ``FakeExecutor``, an in-process implementation of the executor
interface whose "steps" write contract-conforming outputs and provenance (or
deliberately broken ones). One test runs a real step script through the
``subprocess`` executor (POSIX only) to cancel a step while it is running.
All data is synthetic bytes; nothing here resembles an image of a person.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import textwrap
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models import (
    Artifact,
    AuditEvent,
    Case,
    InputFile,
    Notification,
    PipelineRun,
    ReferenceBundle,
    RunSlot,
    RunSlotBundle,
    SlotImplementationSetting,
    StepRun,
    User,
)
from contracts import validate as contract
from orchestrator import bundles as reg
from orchestrator.actions import ActionRefused, cancel_run, resume_run
from orchestrator.bundle_resolver import RegistryBundleResolver
from orchestrator.catalog import StaticPipelineRegistry, StaticStepCatalog
from orchestrator.engine import (
    ExecutorRegistry,
    Orchestrator,
    OrchestratorConfig,
    TickResult,
    stale_runs,
)
from orchestrator.executors.base import (
    Accounting,
    Executor,
    ExecutorHandle,
    JobState,
    JobStatus,
    StepResult,
    Usage,
    build_execution_record,
    classify_exit,
    utc_now,
)
from orchestrator.pipeline import PipelineError, load_pipeline
from orchestrator.resolution import RunRefused, create_run
from orchestrator.store import RunStore

DIGEST = {
    name: hashlib.sha256(name.encode()).hexdigest()
    for name in ("intake", "norm", "disc")
}
IMAGES = {k: f"registry.example.org/test/{k}@sha256:{v}" for k, v in DIGEST.items()}

PIPELINE_YAML = f"""
contract_version: "1.0"
kind: Pipeline
id: test-flow
version: 1.0.0
title: Synthetic test pipeline
inputs:
  - {{name: ct, type: ct_native}}
  - {{name: lesion_native, type: lesion_mask_native}}
  - {{name: lesion_mni, type: lesion_mask_mni_any}}
input_sets:
  - {{name: native, inputs: [ct, lesion_native]}}
  - {{name: mni, inputs: [lesion_mni]}}
slots:
  - name: intake
    consumes:
      - {{port: lesion_mni, type: lesion_mask_mni_any, from: inputs.lesion_mni, optional: true}}
      - {{port: lesion_native, type: lesion_mask_native, from: inputs.lesion_native, optional: true}}
    produces:
      - {{port: lesion_qc, type: lesion_qc}}
      - port: lesion_mask_mni
        type: lesion_mask_mni
        when: {{inputs_present: [lesion_mni]}}
    implementations:
      default: intake
      allowed:
        - id: intake
          step: {{name: intake-step, version: 1.0.0}}
          image: {IMAGES["intake"]}
  - name: normalisation
    when: {{inputs_present: [ct, lesion_native]}}
    consumes:
      - {{port: ct, type: ct_native, from: inputs.ct}}
      - {{port: lesion_mask, type: lesion_mask_native, from: inputs.lesion_native}}
    produces:
      - {{port: lesion_mask_mni, type: lesion_mask_mni}}
    implementations:
      default: norm
      allowed:
        - id: norm
          step: {{name: norm-step, version: 1.0.0}}
          image: {IMAGES["norm"]}
  - name: disconnection
    consumes:
      - port: lesion_mask
        type: lesion_mask_mni
        from: [normalisation.lesion_mask_mni, intake.lesion_mask_mni]
    produces:
      - {{port: tract_disconnection, type: tract_disconnection}}
    implementations:
      default: disc
      allowed:
        - id: disc
          step: {{name: disc-step, version: 1.0.0}}
          image: {IMAGES["disc"]}
"""


def manifest(name, provides, inputs, outputs, *, bundles=(), params=None, image="x"):
    return {
        "contract_version": "1.0",
        "kind": "Step",
        "name": name,
        "version": "1.0.0",
        "title": name,
        "provides": provides,
        "image": {"repository": f"registry.example.org/test/{image}"},
        "licence": {"spdx": "MIT"},
        "inputs": [{"name": n, "type": t, "required": r} for n, t, r in inputs],
        "outputs": [{"name": n, "type": t, "required": r} for n, t, r in outputs],
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": params
            or {"seed": {"type": "integer", "minimum": 0, "default": 0}},
        },
        "bundles": list(bundles),
        "resources": {"cpus": 1, "memory_mb": 256, "wall_time_minutes": 5},
        "errors": [
            {
                "code": "mask_empty",
                "category": "input_rejected",
                "description": "Empty.",
            }
        ],
    }


MANIFESTS = {
    "intake-step": manifest(
        "intake-step",
        "intake",
        [
            ("lesion_mni", "lesion_mask_mni_any", False),
            ("lesion_native", "lesion_mask_native", False),
        ],
        [
            ("lesion_qc", "lesion_qc", True),
            ("lesion_mask_mni", "lesion_mask_mni", False),
        ],
        image="intake",
    ),
    "norm-step": manifest(
        "norm-step",
        "normalisation",
        [("ct", "ct_native", True), ("lesion_mask", "lesion_mask_native", True)],
        [("lesion_mask_mni", "lesion_mask_mni", True)],
        image="norm",
    ),
    "disc-step": manifest(
        "disc-step",
        "disconnection",
        [("lesion_mask", "lesion_mask_mni", True)],
        [("tract_disconnection", "tract_disconnection", True)],
        image="disc",
    ),
}
EXT = {
    "lesion_qc": ".json",
    "lesion_mask_mni": ".nii.gz",
    "tract_disconnection": ".tsv",
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


GRID2 = contract.load_file(contract.DEFAULT_TYPES_PATH)["grids"]["MNI152NLin6Asym-res2"]
MASK_MODES = ("offgrid", "nonbinary", "scaled", "truncated")


def mask_bytes(kind: str = "ok") -> bytes:
    """A synthetic gzip NIfTI-1 mask: a small cube of ones on the 2 mm working grid.

    ``kind`` breaks it on purpose: ``offgrid`` (1 mm voxel sizes), ``nonbinary``
    (one voxel 2), ``truncated`` (voxel data cut short); ``scaled`` stores 0/2
    with ``scl_slope`` 0.5, which is binary after scaling.
    """
    import gzip as _gzip

    import nibabel as nib
    import numpy as np

    data = np.zeros(GRID2["shape"], dtype=np.uint8)
    data[40:44, 50:54, 40:44] = 1
    affine = np.array(GRID2["affine"], dtype=float)
    if kind == "offgrid":
        affine = np.diag([1.0, 1.0, 1.0, 1.0])
    if kind == "nonbinary":
        data[41, 51, 41] = 2
    img = nib.Nifti1Image(data * 2 if kind == "scaled" else data, affine)
    if kind == "scaled":
        img.header.set_slope_inter(0.5, 0)
    raw = img.to_bytes()
    if kind == "truncated":
        raw = raw[: len(raw) // 2]
    return _gzip.compress(raw, mtime=0)


def write_step_outputs(
    out: Path, step: dict, staged: dict[str, Path], *, mode: str
) -> int:
    """What a conforming step would write; ``mode`` breaks it on purpose. Returns the exit code."""
    stamp = "2026-11-13T10:00:00Z"
    inputs = []
    for name, path in sorted(staged.items()):
        port = name.split(".")[0]
        if port == "params":
            continue
        atype = next(i["type"] for i in step["inputs"] if i["name"] == port)
        inputs.append(
            {
                "port": port,
                "type": atype,
                "sha256": _sha(path),
                "size_bytes": path.stat().st_size,
            }
        )
    status, code = "succeeded", 0
    produced = []
    if mode == "reject":
        status, code = "rejected", 10
        (out / "error.json").write_text(
            json.dumps(
                {
                    "contract_version": "1.0",
                    "kind": "StepError",
                    "step": {"name": step["name"], "version": step["version"]},
                    "category": "input_rejected",
                    "code": "mask_empty",
                    "message": "The lesion mask has no voxels set.",
                }
            )
        )
    elif mode == "fail":
        status, code = "failed", 1
    else:
        for o in step["outputs"]:
            if (
                o["name"] == "lesion_mask_mni"
                and step["name"] == "intake-step"
                and "lesion_mni" not in [i["port"] for i in inputs]
            ):
                continue
            if mode == "violate" and o["required"]:
                continue
            target = out / (o["name"] + EXT[o["type"]])
            if o["type"] == "lesion_mask_mni":  # format-checked by the orchestrator
                payload = mask_bytes(mode if mode in MASK_MODES else "ok")
            else:
                payload = (
                    f"{step['name']}:{o['name']}:"
                    f"{sorted(i['sha256'] for i in inputs)}"
                ).encode()
            if mode == "symlink":  # same bytes, but /out holds a link to elsewhere
                real = out.parent / ("elsewhere-" + target.name)
                real.write_bytes(payload)
                target.symlink_to(real)
            else:
                target.write_bytes(payload)
            produced.append(
                {
                    "port": o["name"],
                    "type": o["type"],
                    "sha256": _sha(target),
                    "size_bytes": target.stat().st_size,
                }
            )
    (out / "provenance.json").write_text(
        json.dumps(
            {
                "contract_version": "1.0",
                "kind": "StepProvenance",
                "step": {"name": step["name"], "version": step["version"]},
                "status": status,
                "started_at": stamp,
                "finished_at": stamp,
                "parameters": json.loads((staged["params.json"]).read_text()),
                "inputs": inputs,
                "outputs": produced,
                "bundles": [],
                "software": [{"name": "synthetic", "version": "1"}],
            }
        )
    )
    return code


class FakeExecutor(Executor):
    """In-process executor. ``script[step name]`` is a list of modes consumed per attempt:
    ``ok``, ``fail``, ``reject``, ``violate`` (missing required output) or ``hang``
    (runs until canceled)."""

    name = "docker"
    runtime = "docker"
    restartable = True

    def __init__(self, script: dict[str, list[str]] | None = None) -> None:
        self.script = script or {}
        self.jobs: dict[str, dict] = {}
        self.submitted: list[str] = []
        self.canceled: list[str] = []
        self.cancel_failures = 0
        self.can_adopt = True
        self.adopt_calls: list[str] = []

    def submit(self, invocation):
        self.check_policy(invocation)
        modes = self.script.get(invocation.step_name, [])
        mode = modes.pop(0) if modes else "ok"
        work = invocation.work_dir
        (work / "in").mkdir(parents=True)
        (work / "out").mkdir()
        (work / "logs").mkdir()
        (work / "logs" / "step.log").write_text("INFO fake step\n")
        staged = {}
        for name, src in invocation.inputs.items():
            shutil.copyfile(src, work / "in" / name)
            staged[name] = work / "in" / name
        (work / "in" / "params.json").write_text(json.dumps(dict(invocation.params)))
        staged["params.json"] = work / "in" / "params.json"
        job_id = uuid.uuid4().hex
        self.jobs[job_id] = {
            "mode": mode,
            "inv": invocation,
            "staged": staged,
            "state": "running",
        }
        self.submitted.append(invocation.step_name)
        return ExecutorHandle("docker", job_id, utc_now(), {"work_dir": str(work)})

    def poll(self, handle):
        job = self.jobs[handle.external_job_id]
        if job["state"] == "running" and job["mode"] != "hang":
            out = Path(handle.data["work_dir"]) / "out"
            step = MANIFESTS[job["inv"].step_name]
            job["exit"] = write_step_outputs(out, step, job["staged"], mode=job["mode"])
            job["state"] = "done"
        if job["state"] == "running":
            return JobStatus(JobState.RUNNING, started_at=utc_now())
        return JobStatus(JobState.FINISHED)

    def cancel(self, handle):
        job = self.jobs[handle.external_job_id]
        self.canceled.append(job["inv"].step_name)
        if self.cancel_failures:
            self.cancel_failures -= 1
            raise RuntimeError("executor backend unreachable")
        if job["state"] != "running":
            return False
        job["state"], job["stop"] = "done", "canceled"
        job["exit"] = None
        return True

    def collect(self, handle):
        job = self.jobs[handle.external_job_id]
        outcome = classify_exit(job.get("exit"), stop_reason=job.get("stop"))
        work = Path(handle.data["work_dir"])
        out = work / "out"
        outputs = {
            p.name: p
            for p in (out.iterdir() if out.is_dir() else ())
            if p.is_file() and p.name not in ("provenance.json", "error.json")
        }
        return StepResult(
            outcome=outcome,
            exit_code=job.get("exit"),
            signal=None,
            out_dir=out,
            outputs={} if outcome.interrupted else outputs,
            extra_dir=None,
            provenance=None,
            error=None,
            log_path=work / "logs" / "step.log",
            usage=Usage(wall_seconds=1.5),
            execution=build_execution_record(
                executor="docker",
                runtime="docker",
                invocation=job["inv"],
                outcome=outcome,
                exit_code=job.get("exit"),
                queued_at=handle.submitted_at,
                started_at=handle.submitted_at,
                finished_at=utc_now(),
                usage=Usage(wall_seconds=1.5),
            ),
        )

    def cleanup(self, handle):
        work = Path(handle.data["work_dir"])
        for name in ("in", "out"):
            shutil.rmtree(work / name, ignore_errors=True)

    def adopt(self, invocation):
        """Find a job by attempt, like the real executors (``can_adopt`` off: the
        interface default)."""
        self.adopt_calls.append(invocation.step_name)
        if not self.can_adopt:
            return super().adopt(invocation)
        for job_id, job in self.jobs.items():
            inv = job["inv"]
            if (inv.step_run_id, inv.attempt, inv.work_dir) == (
                invocation.step_run_id,
                invocation.attempt,
                invocation.work_dir,
            ):
                return ExecutorHandle(
                    "docker", job_id, utc_now(), {"work_dir": str(inv.work_dir)}
                )
        return None


# ----------------------------------------------------------------------- fixtures


class Env:
    def __init__(
        self, tmp_path: Path, script=None, executor=None, config=None, bundles=None
    ):
        self.root = tmp_path / "store"
        engine = create_engine(f"sqlite:///{tmp_path / 'orch.db'}")
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, autoflush=False)
        self.pipeline = load_pipeline(PIPELINE_YAML.encode())
        self.pipelines = StaticPipelineRegistry([self.pipeline])
        self.steps = StaticStepCatalog(MANIFESTS.values())
        self.executor = executor or FakeExecutor(script)
        self.bundles = bundles or RegistryBundleResolver(
            tmp_path / "bundles", self.Session
        )
        self.orch = Orchestrator(
            self.Session,
            pipelines=self.pipelines,
            steps=self.steps,
            executors=ExecutorRegistry(
                {self.executor.name: self.executor}, self.executor.name
            ),
            bundles=self.bundles,
            store=RunStore(self.root),
            settings=_Settings(),
            config=config or OrchestratorConfig(poll_seconds=0, max_attempts=2),
        )
        with self.Session() as db:
            self.user = User(
                email="owner@example.org", full_name="Owner", role="researcher"
            )
            admin = User(email="admin@example.org", full_name="Admin", role="admin")
            db.add_all([self.user, admin])
            for impl, slot in (
                ("intake", "intake"),
                ("norm", "normalisation"),
                ("disc", "disconnection"),
            ):
                db.add(
                    SlotImplementationSetting(
                        pipeline_id="test-flow",
                        slot=slot,
                        implementation_id=impl,
                        enabled=True,
                    )
                )
            db.commit()
            self.user_id = self.user.id

    def make_case(self, roles=("ct", "lesion_native"), age_band=None):
        types = self.pipeline.inputs
        with self.Session() as db:
            case = Case(
                owner_user_id=self.user_id,
                deployment_profile="research-public",
                age_band=age_band,
            )
            db.add(case)
            db.flush()
            files = {}
            for role in roles:
                rel = f"{case.id}/rawdata/{role}.bin"
                path = self.root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(f"synthetic {role}".encode())
                f = InputFile(
                    case_id=case.id,
                    role=role,
                    artefact_type=types[role],
                    storage_path=rel,
                    sha256=_sha(path),
                    size_bytes=path.stat().st_size,
                    uploaded_by_user_id=self.user_id,
                )
                db.add(f)
                files[role] = f
            db.commit()
            return case.id

    def create(self, case_id, **kw):
        with self.Session() as db:
            case = db.get(Case, case_id)
            inputs = {
                f.role: f
                for f in db.scalars(
                    select(InputFile).where(InputFile.case_id == case_id)
                )
            }
            run = create_run(
                db,
                case=case,
                pipeline=self.pipeline,
                inputs=inputs,
                created_by_user_id=self.user_id,
                steps=self.steps,
                bundles=self.bundles,
                accounting=Accounting(submitted_by=f"user:{self.user_id}"),
                platform_version="0.0.0-test",
                seed=7,
                **kw,
            )
            db.commit()
            return run.id

    def drive(self, run_id, owner="task-1", max_ticks=50):
        for _ in range(max_ticks):
            result = self.orch.tick(run_id, owner)
            if result is not TickResult.CONTINUE:
                return result
        raise AssertionError("run did not finish")

    def run(self, run_id):
        db = self.Session()
        return db, db.get(PipelineRun, run_id)

    def attempts(self, run_id):
        with self.Session() as db:
            rows = db.execute(
                select(
                    RunSlot.slot,
                    StepRun.attempt,
                    StepRun.trigger,
                    StepRun.status,
                    StepRun.outcome,
                )
                .join(RunSlot, RunSlot.id == StepRun.run_slot_id)
                .where(StepRun.pipeline_run_id == run_id)
                .order_by(StepRun.queued_at, StepRun.attempt)
            ).all()
            return [tuple(r) for r in rows]


class _Settings:
    notifications_email_enabled = False
    smtp_host = None
    smtp_from_email = None
    app_public_base_url = "http://localhost"


# -------------------------------------------------------------------------- tests


def test_pipeline_loading_validates_and_orders():
    pipeline = load_pipeline(PIPELINE_YAML.encode())
    assert pipeline.topological_order == ["intake", "normalisation", "disconnection"]
    assert pipeline.sha256 == hashlib.sha256(PIPELINE_YAML.encode()).hexdigest()
    assert pipeline.match_input_set(["lesion_mni"]) == "mni"
    with pytest.raises(PipelineError):
        load_pipeline(
            PIPELINE_YAML.replace(
                "lesion_mask_mni_any, from: inputs.lesion_mni",
                "ct_native, from: inputs.lesion_mni",
            ).encode()
        )
    example = Path(contract.CONTRACTS_DIR) / "examples" / "pipeline-stroke-lnm.yaml"
    assert load_pipeline(example).id == "stroke-lnm"


def test_success_records_slots_attempts_artefacts_provenance_and_notifies(tmp_path):
    env = Env(tmp_path)
    run_id = env.create(env.make_case())
    assert env.drive(run_id) is TickResult.FINISHED
    db, run = env.run(run_id)
    assert run.status == "succeeded" and run.finished_at and run.started_at
    assert env.attempts(run_id) == [
        ("intake", 1, "initial", "succeeded", "succeeded"),
        ("normalisation", 1, "initial", "succeeded", "succeeded"),
        ("disconnection", 1, "initial", "succeeded", "succeeded"),
    ]
    slots = {
        s.slot: s
        for s in db.scalars(select(RunSlot).where(RunSlot.pipeline_run_id == run_id))
    }
    assert slots["intake"].chosen_by == "pipeline_default"
    assert slots["normalisation"].effective_parameters == {"seed": 7}
    arts = db.scalars(select(Artifact).where(Artifact.pipeline_run_id == run_id)).all()
    assert sorted(a.port for a in arts) == [
        "lesion_mask_mni",
        "lesion_qc",
        "tract_disconnection",
    ]
    for a in arts:
        stored = env.root / a.storage_path
        assert stored.is_file() and _sha(stored) == a.sha256
        assert "/derivatives/" in a.storage_path
    # disconnection consumed normalisation's mask (first available source)
    steps = db.scalars(select(StepRun).where(StepRun.pipeline_run_id == run_id)).all()
    disc = next(s for s in steps if s.step_provenance["step"]["name"] == "disc-step")
    norm_mask = next(a for a in arts if a.port == "lesion_mask_mni")
    assert disc.step_provenance["inputs"][0]["sha256"] == norm_mask.sha256
    assert all(s.log_path and s.step_provenance_sha256 for s in steps)
    record = json.loads((env.root / run.provenance_path).read_text())
    assert record["status"] == "succeeded" and len(record["steps"]) == 3
    assert not contract.schema_errors(record, "provenance")
    kinds = {n.event_type for n in db.scalars(select(Notification))}
    assert kinds == {"run_succeeded"}
    assert (
        db.scalar(
            select(AuditEvent.event_type).where(AuditEvent.pipeline_run_id == run_id)
        )
        == "run_succeeded"
    )
    # work directories are cleaned; logs are kept
    work = env.orch.store.work_root(run.case_id, run_id)
    assert not list(work.rglob("out")) and list(work.rglob("step.log"))
    db.close()


def test_conditional_branch_skips_normalisation_in_mni_mode(tmp_path):
    env = Env(tmp_path)
    run_id = env.create(env.make_case(roles=("lesion_mni",)))
    db, run = env.run(run_id)
    slots = {
        s.slot: (s.status, s.reason)
        for s in db.scalars(select(RunSlot).where(RunSlot.pipeline_run_id == run_id))
    }
    assert run.input_set == "mni"
    assert slots["normalisation"] == ("skipped", "condition_false")
    db.close()
    assert env.drive(run_id) is TickResult.FINISHED
    assert [a[0] for a in env.attempts(run_id)] == ["intake", "disconnection"]
    db, run = env.run(run_id)
    assert run.status == "succeeded"
    intake_mask = db.scalar(
        select(Artifact).where(
            Artifact.pipeline_run_id == run_id, Artifact.port == "lesion_mask_mni"
        )
    )
    disc = db.scalar(
        select(StepRun)
        .join(RunSlot, RunSlot.id == StepRun.run_slot_id)
        .where(RunSlot.slot == "disconnection")
    )
    assert disc.step_provenance["inputs"][0]["sha256"] == intake_mask.sha256
    db.close()


def test_mid_pipeline_failure_then_resume_keeps_succeeded_slots(tmp_path):
    env = Env(tmp_path, script={"norm-step": ["fail", "fail"]})
    run_id = env.create(env.make_case())
    assert env.drive(run_id) is TickResult.FINISHED
    db, run = env.run(run_id)
    assert run.status == "failed" and run.failure_code == "step_failed"
    failed = db.get(StepRun, run.failed_step_run_id)
    assert failed.trigger == "auto_retry" and failed.outcome == "failed"
    db.close()
    assert env.attempts(run_id) == [
        ("intake", 1, "initial", "succeeded", "succeeded"),
        ("normalisation", 1, "initial", "failed", "failed"),
        ("normalisation", 2, "auto_retry", "failed", "failed"),
    ]
    assert "disc-step" not in env.executor.submitted
    with env.Session() as db:
        assert {n.event_type for n in db.scalars(select(Notification))} == {
            "run_failed"
        }
        assert db.scalar(select(Notification.case_id)) is not None

    with env.Session() as db:
        result = resume_run(
            db,
            run_id,
            env.user_id,
            pipelines=env.pipelines,
            steps=env.steps,
            bundles=env.bundles,
        )
        db.commit()
    assert result.enqueue and result.status == "queued"
    assert env.drive(run_id, owner="task-2") is TickResult.FINISHED
    db, run = env.run(run_id)
    assert (
        run.status == "succeeded" and run.resume_count == 1 and run.failure_code is None
    )
    db.close()
    assert env.attempts(run_id)[3:] == [
        ("normalisation", 3, "user_resume", "succeeded", "succeeded"),
        ("disconnection", 1, "initial", "succeeded", "succeeded"),
    ]
    assert env.executor.submitted.count("intake-step") == 1  # kept, not rerun
    with env.Session() as db, pytest.raises(ActionRefused) as exc:
        resume_run(
            db,
            run_id,
            env.user_id,
            pipelines=env.pipelines,
            steps=env.steps,
            bundles=env.bundles,
        )
    assert exc.value.code == "run_succeeded"


def test_rejection_is_not_retried_and_blocks_resume(tmp_path):
    env = Env(tmp_path, script={"intake-step": ["reject"]})
    run_id = env.create(env.make_case())
    env.drive(run_id)
    db, run = env.run(run_id)
    assert run.status == "failed" and run.failure_code == "input_rejected"
    assert "no voxels" in run.failure_message
    step = db.get(StepRun, run.failed_step_run_id)
    assert step.outcome == "rejected" and step.step_error["code"] == "mask_empty"
    with pytest.raises(ActionRefused) as exc:
        resume_run(
            db,
            run_id,
            env.user_id,
            pipelines=env.pipelines,
            steps=env.steps,
            bundles=env.bundles,
        )
    assert exc.value.code == "input_rejected"
    db.close()
    # Not retried; the independent normalisation slot was allowed to finish.
    assert [a for a in env.attempts(run_id) if a[0] == "intake"] == [
        ("intake", 1, "initial", "failed", "rejected")
    ]
    assert "disc-step" not in env.executor.submitted


def test_contract_violation_is_detected_and_not_retried(tmp_path):
    env = Env(tmp_path, script={"intake-step": ["violate"]})
    run_id = env.create(env.make_case())
    env.drive(run_id)
    assert env.attempts(run_id)[0] == (
        "intake",
        1,
        "initial",
        "failed",
        "contract_violation",
    )
    assert [a[0] for a in env.attempts(run_id)].count("intake") == 1
    with env.Session() as db:
        step = db.scalar(select(StepRun).where(StepRun.outcome == "contract_violation"))
        assert step.failure_code == "contract_violation_missing_output"
        assert (
            db.scalar(select(Artifact.id).where(Artifact.step_run_id == step.id))
            is None
        )
        assert db.get(PipelineRun, run_id).status == "failed"


def test_cancellation_while_a_step_runs(tmp_path):
    env = Env(tmp_path, script={"norm-step": ["hang"]})
    run_id = env.create(env.make_case())
    for _ in range(5):
        env.orch.tick(run_id, "task-1")
    assert env.attempts(run_id)[-1] == ("normalisation", 1, "initial", "running", None)
    with env.Session() as db:
        result = cancel_run(db, run_id, env.user_id)
        db.commit()
        assert result.cancel_requested and result.status == "running"
        again = cancel_run(db, run_id, env.user_id)
        assert not again.changed
    assert env.drive(run_id) is TickResult.FINISHED
    db, run = env.run(run_id)
    assert run.status == "canceled" and run.finished_at is not None
    assert env.attempts(run_id)[-1] == (
        "normalisation",
        1,
        "initial",
        "canceled",
        "canceled",
    )
    assert env.executor.canceled == ["norm-step"]
    assert db.scalar(select(Notification.id)) is None
    db.close()
    # a canceled run can be resumed and finishes
    with env.Session() as db:
        resume_run(
            db,
            run_id,
            env.user_id,
            pipelines=env.pipelines,
            steps=env.steps,
            bundles=env.bundles,
        )
        db.commit()
    env.drive(run_id, owner="task-2")
    db, run = env.run(run_id)
    assert run.status == "succeeded"
    db.close()


def test_cancel_of_a_queued_run_and_refusals(tmp_path):
    env = Env(tmp_path)
    run_id = env.create(env.make_case())
    with env.Session() as db:
        result = cancel_run(db, run_id, env.user_id)
        db.commit()
    assert result.revoke_task and result.status == "canceled"
    assert env.orch.tick(run_id, "task-1") is TickResult.FINISHED
    assert env.attempts(run_id) == []


def test_run_creation_refusals_and_user_choice(tmp_path):
    env = Env(tmp_path)
    case_id = env.make_case()
    with pytest.raises(RunRefused) as exc:
        env.create(case_id, requested_implementations={"normalisation": "missing"})
    assert exc.value.code == "implementation_not_enabled"
    with env.Session() as db:
        row = db.scalar(
            select(SlotImplementationSetting).where(
                SlotImplementationSetting.slot == "disconnection"
            )
        )
        row.enabled = False
        db.commit()
    with pytest.raises(RunRefused) as exc:
        env.create(case_id)
    assert exc.value.code == "no_implementation" and exc.value.slot == "disconnection"
    with env.Session() as db:
        row = db.scalar(
            select(SlotImplementationSetting).where(
                SlotImplementationSetting.slot == "disconnection"
            )
        )
        row.enabled = True
        db.commit()
    run_id = env.create(case_id, requested_implementations={"normalisation": "norm"})
    with env.Session() as db:
        slot = db.scalar(
            select(RunSlot).where(
                RunSlot.pipeline_run_id == run_id, RunSlot.slot == "normalisation"
            )
        )
        assert slot.chosen_by == "user"
    with pytest.raises(Exception):  # one active run per case
        env.create(case_id)


def test_lease_prevents_two_drivers_and_stale_runs_are_found(tmp_path):
    clock = {"now": None}
    env = Env(tmp_path, script={"norm-step": ["hang"]})
    run_id = env.create(env.make_case())
    assert env.orch.tick(run_id, "task-1") is TickResult.CONTINUE
    assert env.orch.tick(run_id, "task-2") is TickResult.LOST
    with env.Session() as db:
        assert stale_runs(db, 600) == []
        from datetime import datetime, timedelta, timezone

        clock["now"] = datetime.now(timezone.utc) + timedelta(seconds=601)
        assert stale_runs(db, 600, now=clock["now"]) == [run_id]
    env.orch.clock = lambda: clock["now"]
    assert env.orch.tick(run_id, "task-2") is TickResult.CONTINUE


def make_bundle(dest: Path, bundle_id="toy-tracts", btype="tract-atlas") -> Path:
    dest.mkdir(parents=True)
    data = b"synthetic tract atlas"
    (dest / "atlas.txt").write_bytes(data)
    entries = [
        {
            "path": "atlas.txt",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
        }
    ]
    (dest / "bundle.yaml").write_text(
        yaml.safe_dump(
            {
                "contract_version": "1.0",
                "kind": "Bundle",
                "id": bundle_id,
                "version": "1.0.0",
                "type": btype,
                "title": "Synthetic tracts",
                "synthetic": True,
                "licence": {"spdx": "CC0-1.0", "redistributable": True},
                "content_digest": contract.bundle_digest(entries),
                "files": entries,
            }
        )
    )
    return dest


def _with_bundle_requirement(monkeypatch):
    disc = dict(MANIFESTS["disc-step"])
    disc["bundles"] = [{"name": "tracts", "type": "tract-atlas"}]
    monkeypatch.setitem(MANIFESTS, "disc-step", disc)


def test_bundles_resolved_through_the_registry_and_mounted(tmp_path, monkeypatch):
    _with_bundle_requirement(monkeypatch)
    env = Env(tmp_path)
    case_id = env.make_case()
    with pytest.raises(RunRefused) as exc:
        env.create(case_id)
    assert exc.value.code == "bundle_unavailable"
    with env.Session() as db:
        reg.BundleRegistry(tmp_path / "bundles", reg.SqlAlchemyStore(db)).register(
            make_bundle(tmp_path / "src")
        )
    run_id = env.create(case_id)
    with env.Session() as db:
        link = db.scalar(select(RunSlotBundle))
        assert (
            db.get(ReferenceBundle, link.reference_bundle_id).bundle_id == "toy-tracts"
        )

    mounted = []
    original = env.executor.submit

    def submit(invocation):
        mounted.extend(
            (b.name, b.id, Path(b.host_path).is_dir()) for b in invocation.bundles
        )
        return original(invocation)

    env.executor.submit = submit
    # The fake step reports no bundles: the orchestrator detects the mismatch.
    env.drive(run_id)
    assert mounted == [("tracts", "toy-tracts", True)]
    assert env.attempts(run_id)[-1] == (
        "disconnection",
        1,
        "initial",
        "failed",
        "contract_violation",
    )


def test_tampered_bundle_fails_the_attempt(tmp_path, monkeypatch):
    _with_bundle_requirement(monkeypatch)
    env = Env(tmp_path)
    with env.Session() as db:
        reg.BundleRegistry(tmp_path / "bundles", reg.SqlAlchemyStore(db)).register(
            make_bundle(tmp_path / "src")
        )
    run_id = env.create(env.make_case())
    (tmp_path / "bundles" / "toy-tracts" / "1.0.0" / "atlas.txt").write_bytes(
        b"tampered!"
    )
    env.drive(run_id)
    with env.Session() as db:
        step = db.scalar(
            select(StepRun).where(StepRun.failure_code == "bundle_unavailable")
        )
        assert step is not None and step.status == "failed"
        assert db.get(PipelineRun, run_id).status == "failed"
    assert "disc-step" not in env.executor.submitted


def test_executor_cancel_failure_is_retried_on_later_ticks(tmp_path):
    env = Env(tmp_path, script={"norm-step": ["hang"]})
    env.executor.cancel_failures = 1
    run_id = env.create(env.make_case())
    for _ in range(5):
        env.orch.tick(run_id, "task-1")
    with env.Session() as db:
        cancel_run(db, run_id, env.user_id)
        db.commit()
    # First tick: the executor's cancel raises; the attempt keeps running.
    assert env.orch.tick(run_id, "task-1") is TickResult.CONTINUE
    assert env.attempts(run_id)[-1][3] == "running"
    with env.Session() as db:
        step = db.scalar(select(StepRun).where(StepRun.status == "running"))
        assert step.cancel_requested_at is not None
        assert not step.executor_state.get("cancel_sent")
    # Later ticks retry the cancel until it is accepted, then the run ends.
    assert env.drive(run_id) is TickResult.FINISHED
    assert env.executor.canceled == ["norm-step", "norm-step"]
    db, run = env.run(run_id)
    assert run.status == "canceled"
    db.close()


def test_lease_lost_during_submit_discards_the_new_job(tmp_path):
    env = Env(tmp_path)
    run_id = env.create(env.make_case())
    original = env.executor.submit
    stolen = []

    def submit_then_lose_lease(invocation):
        handle = original(invocation)
        if not stolen:  # another task takes the run over while we submit
            stolen.append(invocation.step_name)
            with env.Session() as other:
                other.execute(
                    update(PipelineRun)
                    .where(PipelineRun.id == run_id)
                    .values(
                        orchestrator_task_id="task-2",
                        updated_at=datetime.now(timezone.utc),
                    )
                )
                other.commit()
        return handle

    env.executor.submit = submit_then_lose_lease
    assert env.orch.tick(run_id, "task-1") is TickResult.LOST
    first = stolen[0]
    assert env.executor.canceled == [first]  # the unrecorded job was stopped
    with env.Session() as db:
        orphan = db.scalar(
            select(StepRun).where(
                StepRun.attempt == 1, StepRun.executor_state.is_(None)
            )
        )
        assert orphan is not None and orphan.status == "queued"
    # The new owner sees a used work directory with no handle (lost submission),
    # fails that attempt and retries it automatically; the run still succeeds.
    assert env.drive(run_id, owner="task-2") is TickResult.FINISHED
    db, run = env.run(run_id)
    assert run.status == "succeeded"
    lost = db.scalar(select(StepRun).where(StepRun.failure_code == "lost_submission"))
    assert lost is not None
    retry = db.scalar(
        select(StepRun).where(
            StepRun.run_slot_id == lost.run_slot_id, StepRun.attempt == 2
        )
    )
    assert (retry.trigger, retry.status) == ("auto_retry", "succeeded")
    db.close()


def test_heartbeat_is_renewed_during_a_tick(tmp_path):
    env = Env(tmp_path, config=OrchestratorConfig(poll_seconds=0, lease_seconds=600))
    base = datetime.now(timezone.utc)
    calls = {"n": 0}

    def slow_clock():  # every clock read is 100 s later: one tick spans > lease
        calls["n"] += 1
        return base + timedelta(seconds=100 * calls["n"])

    env.orch.clock = slow_clock
    run_id = env.create(env.make_case())
    assert env.orch.tick(run_id, "task-1") is TickResult.CONTINUE
    assert calls["n"] > 6  # the tick read the clock well past the lease length
    with env.Session() as db:
        # The heartbeat is recent relative to the tick's own end, not its start.
        assert stale_runs(db, 600, now=slow_clock()) == []


@pytest.mark.skipif(os.name != "posix", reason="symbolic links need POSIX here")
def test_symlink_in_out_is_a_contract_violation(tmp_path):
    env = Env(tmp_path, script={"intake-step": ["symlink"]})
    run_id = env.create(env.make_case())
    env.drive(run_id)
    with env.Session() as db:
        step = db.scalar(select(StepRun).where(StepRun.outcome == "contract_violation"))
        assert step is not None and step.failure_code == "contract_violation_symlink"
        assert (
            db.scalar(select(Artifact.id).where(Artifact.step_run_id == step.id))
            is None
        )


# ------------------------------------------- lost submissions and adoption (ORCH-01)


class DriverCrash(Exception):
    """Stands for the worker dying between submit and recording the handle."""


def _crash_after_submit(env, step_name, *, start_job=True):
    """Make the first submission of ``step_name`` leave a used work directory and
    no recorded handle, with (``start_job``) or without a job behind it."""
    original = env.executor.submit
    crashed = []

    def submit(invocation):
        if invocation.step_name == step_name and not crashed:
            crashed.append(invocation.step_name)
            if start_job:
                original(invocation)
            else:  # stopped while staging: nothing was started
                (invocation.work_dir / "in").mkdir(parents=True)
            raise DriverCrash()
        return original(invocation)

    env.executor.submit = submit
    return crashed


def _tick_through_crash(env, run_id, owner="task-1"):
    with pytest.raises(DriverCrash):
        for _ in range(5):
            env.orch.tick(run_id, owner)


def test_lost_submission_with_a_live_job_is_adopted_not_rerun(tmp_path):
    env = Env(tmp_path)
    run_id = env.create(env.make_case())
    _crash_after_submit(env, "intake-step")
    _tick_through_crash(env, run_id)
    with env.Session() as db:
        orphan = db.scalar(select(StepRun).where(StepRun.executor_state.is_(None)))
        assert orphan is not None and orphan.status == "queued"

    # The restarted driver finds the job through the executor and adopts it.
    assert env.drive(run_id) is TickResult.FINISHED
    db, run = env.run(run_id)
    assert run.status == "succeeded"
    intake = db.get(StepRun, orphan.id)
    assert (intake.attempt, intake.status, intake.outcome) == (
        1,
        "succeeded",
        "succeeded",
    )
    assert intake.executor_state["adopted"] is True
    assert intake.external_job_id == intake.executor_state["handle"]["external_job_id"]
    db.close()
    assert env.executor.adopt_calls == ["intake-step"]
    assert env.executor.submitted.count("intake-step") == 1  # not started twice
    assert [a for a in env.attempts(run_id) if a[0] == "intake"] == [
        ("intake", 1, "initial", "succeeded", "succeeded")
    ]


def test_lost_submission_without_a_job_fails_and_is_retried(tmp_path):
    env = Env(tmp_path)
    run_id = env.create(env.make_case())
    _crash_after_submit(env, "intake-step", start_job=False)
    _tick_through_crash(env, run_id)
    assert env.drive(run_id) is TickResult.FINISHED
    assert [a for a in env.attempts(run_id) if a[0] == "intake"] == [
        ("intake", 1, "initial", "failed", "failed"),
        ("intake", 2, "auto_retry", "succeeded", "succeeded"),
    ]
    with env.Session() as db:
        lost = db.scalar(select(StepRun).where(StepRun.status == "failed"))
        assert lost.failure_code == "lost_submission"
    assert env.executor.adopt_calls == ["intake-step"]


def test_lost_submission_with_an_executor_that_cannot_adopt(tmp_path):
    env = Env(tmp_path)
    env.executor.can_adopt = False  # the interface default: NotImplementedError
    run_id = env.create(env.make_case())
    _crash_after_submit(env, "intake-step")
    _tick_through_crash(env, run_id)
    assert env.drive(run_id) is TickResult.FINISHED
    assert [a[2:] for a in env.attempts(run_id) if a[0] == "intake"] == [
        ("initial", "failed", "failed"),
        ("auto_retry", "succeeded", "succeeded"),
    ]


def test_adopted_job_that_ends_canceled_without_a_cancel_request_is_retried(tmp_path):
    """The lease-loss path cancels the job it could not record; the next owner
    adopts it, and the canceled job counts as a lost submission, not a user
    cancellation."""
    env = Env(tmp_path)
    run_id = env.create(env.make_case())
    _crash_after_submit(env, "intake-step")
    _tick_through_crash(env, run_id)
    job = next(iter(env.executor.jobs.values()))
    job["state"], job["stop"], job["exit"] = "done", "canceled", None
    assert env.drive(run_id) is TickResult.FINISHED
    db, run = env.run(run_id)
    assert run.status == "succeeded"
    lost = db.scalar(select(StepRun).where(StepRun.status == "failed"))
    assert (lost.outcome, lost.failure_code) == ("failed", "lost_submission")
    assert lost.executor_state["adopted"] is True
    db.close()


# ------------------------------------------------- legacy runs are never stamped


def test_claim_does_not_stamp_legacy_or_final_runs(tmp_path):
    env = Env(tmp_path)
    run_id = env.create(env.make_case())
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with env.Session() as db:
        db.execute(
            update(PipelineRun)
            .where(PipelineRun.id == run_id)
            .values(
                pipeline_id="legacy",
                orchestrator_task_id="celery-legacy",
                updated_at=old,
            )
        )
        db.commit()
    assert env.orch.tick(run_id, "task-1") is TickResult.FINISHED
    db, run = env.run(run_id)
    assert run.orchestrator_task_id == "celery-legacy"
    assert run.updated_at.replace(tzinfo=timezone.utc) == old
    db.close()

    other = env.create(env.make_case())
    assert env.drive(other) is TickResult.FINISHED  # succeeded, owner task-1
    assert env.orch.tick(other, "task-9") is TickResult.FINISHED
    db, run = env.run(other)
    assert run.status == "succeeded" and run.orchestrator_task_id == "task-1"
    db.close()
    assert env.orch.tick(uuid.uuid4(), "task-1") is TickResult.FINISHED


# --------------------------------------------- pipeline unavailable, live attempts


def test_unavailable_pipeline_with_live_attempts_fails_after_the_grace(tmp_path):
    clock = {"now": datetime.now(timezone.utc)}
    env = Env(
        tmp_path,
        script={"norm-step": ["hang", "hang"]},
        config=OrchestratorConfig(
            poll_seconds=0, max_attempts=2, pipeline_unavailable_grace_seconds=600
        ),
    )
    env.orch.clock = lambda: clock["now"]
    run_id = env.create(env.make_case())
    for _ in range(3):
        env.orch.tick(run_id, "task-1")
    assert env.attempts(run_id)[-1][3] == "running"

    # A worker without the pipeline: it leaves the run alone during the grace.
    env.orch.pipelines = StaticPipelineRegistry([])
    assert env.orch.tick(run_id, "task-1") is TickResult.LOST
    with env.Session() as db:
        live = db.scalar(select(StepRun).where(StepRun.status == "running"))
        assert "pipeline_unavailable_since" in live.executor_state

    # The pipeline comes back: the run continues and the grace count restarts.
    env.orch.pipelines = env.pipelines
    assert env.orch.tick(run_id, "task-1") is TickResult.CONTINUE
    with env.Session() as db:
        live = db.scalar(select(StepRun).where(StepRun.status == "running"))
        assert "pipeline_unavailable_since" not in live.executor_state

    # Gone again, for longer than the grace: attempts stopped, run failed.
    env.orch.pipelines = StaticPipelineRegistry([])
    assert env.orch.tick(run_id, "task-1") is TickResult.LOST
    clock["now"] += timedelta(seconds=300)
    assert env.orch.tick(run_id, "task-1") is TickResult.LOST  # still within it
    clock["now"] += timedelta(seconds=301)
    assert env.drive(run_id) is TickResult.FINISHED
    assert env.executor.canceled == ["norm-step"]
    db, run = env.run(run_id)
    assert run.status == "failed" and run.failure_code == "pipeline_unavailable"
    norm = db.scalar(
        select(StepRun)
        .join(RunSlot, RunSlot.id == StepRun.run_slot_id)
        .where(RunSlot.slot == "normalisation")
    )
    assert (norm.status, norm.outcome, norm.failure_code) == (
        "failed",
        "failed",
        "pipeline_unavailable",
    )
    assert norm.executor_state["cancel_sent"] is True
    assert not db.scalar(
        select(StepRun.id).where(StepRun.status.in_(("queued", "running")))
    )
    db.close()
    with env.Session() as db:
        assert {n.event_type for n in db.scalars(select(Notification))} == {
            "run_failed"
        }


# ------------------------------------------------ one step identity per slot


def test_a_step_shared_by_two_slots_is_refused():
    shared = PIPELINE_YAML.replace(
        "step: {name: disc-step, version: 1.0.0}",
        "step: {name: norm-step, version: 1.0.0}",
    )
    with pytest.raises(PipelineError, match="derivatives/norm-step-1.0.0/"):
        load_pipeline(shared.encode())
    # Another version of the same step is a different derivative dataset.
    load_pipeline(
        PIPELINE_YAML.replace(
            "step: {name: disc-step, version: 1.0.0}",
            "step: {name: norm-step, version: 2.0.0}",
        ).encode()
    )
    # Configured variants of one step inside one slot stay allowed.
    variant = PIPELINE_YAML.replace(
        f"""          image: {IMAGES["norm"]}
""",
        f"""          image: {IMAGES["norm"]}
        - id: norm-variant
          step: {{name: norm-step, version: 1.0.0}}
          image: {IMAGES["norm"]}
          parameters: {{seed: 3}}
""",
    )
    assert "norm-variant" in variant
    load_pipeline(variant.encode())


def test_run_creation_refuses_a_definition_built_without_the_loader(tmp_path):
    from orchestrator.pipeline import PipelineDefinition

    env = Env(tmp_path)
    data = yaml.safe_load(
        PIPELINE_YAML.replace(
            "step: {name: disc-step, version: 1.0.0}",
            "step: {name: norm-step, version: 1.0.0}",
        )
    )
    env.pipeline = PipelineDefinition(data=data, sha256="0" * 64)
    with pytest.raises(RunRefused) as exc:
        env.create(env.make_case())
    assert exc.value.code == "step_shared_by_slots"


# ------------------------------------------------- output format checks


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("offgrid", "contract_violation_nifti_grid"),
        ("nonbinary", "contract_violation_nifti_binary"),
        ("truncated", "contract_violation_nifti_unreadable"),
    ],
)
def test_working_grid_mask_outputs_are_format_checked(tmp_path, mode, code):
    env = Env(tmp_path, script={"norm-step": [mode]})
    run_id = env.create(env.make_case())
    env.drive(run_id)
    with env.Session() as db:
        step = db.scalar(select(StepRun).where(StepRun.outcome == "contract_violation"))
        assert step is not None and step.failure_code == code
        assert "lesion_mask_mni" in step.failure_message
        assert str(tmp_path) not in step.failure_message
        assert (
            db.scalar(select(Artifact.id).where(Artifact.step_run_id == step.id))
            is None
        )
        assert db.get(PipelineRun, run_id).status == "failed"
    # contract violations are never retried
    assert [a[0] for a in env.attempts(run_id)].count("normalisation") == 1


def test_scaled_binary_mask_is_accepted(tmp_path):
    env = Env(tmp_path, script={"norm-step": ["scaled"]})
    run_id = env.create(env.make_case())
    assert env.drive(run_id) is TickResult.FINISHED
    db, run = env.run(run_id)
    assert run.status == "succeeded"
    db.close()


def test_check_output_format_rules(tmp_path, monkeypatch):
    import gzip as _gzip

    import nibabel as nib
    import numpy as np

    from orchestrator import collection
    from orchestrator.pipeline import artefact_types

    _v, types = artefact_types()
    path = tmp_path / "x.nii.gz"

    def check(type_name):
        return collection.check_output_format(path, types[type_name], "port_a")

    path.write_bytes(b"plain bytes, not gzip")
    assert check("ct_native") is None  # no constraints in the registry
    assert check("lesion_qc") is None  # not NIfTI
    assert check("lesion_mask_mni")[0] == "nifti_unreadable"
    path.write_bytes(_gzip.compress(b"gzip, but not NIfTI"))
    assert check("lesion_mask_native")[0] == "nifti_unreadable"

    # Binary-only type on any grid; a violation in a later chunk is found.
    monkeypatch.setattr(collection, "SCAN_CHUNK_BYTES", 1000)
    data = np.zeros((20, 30, 40), dtype=np.int16)
    data[0, 0, 0] = 1
    path.write_bytes(_gzip.compress(nib.Nifti1Image(data, np.eye(4)).to_bytes()))
    assert check("lesion_mask_native") is None
    data[19, 29, 39] = -1
    path.write_bytes(_gzip.compress(nib.Nifti1Image(data, np.eye(4)).to_bytes()))
    assert check("lesion_mask_native") == (
        "nifti_binary",
        "Output 'port_a' must contain only the values 0 and 1.",
    )
    floats = np.zeros((4, 4, 4), dtype=np.float32)
    floats[1, 1, 1] = np.nan
    path.write_bytes(_gzip.compress(nib.Nifti1Image(floats, np.eye(4)).to_bytes()))
    assert check("lesion_mask_native")[0] == "nifti_binary"

    # Grid-only type (1 mm QC grid): header checks, values are not read.
    grid1 = contract.load_file(contract.DEFAULT_TYPES_PATH)["grids"][
        "MNI152NLin6Asym-res1"
    ]
    ct = np.full(grid1["shape"], 7, dtype=np.uint8)
    affine = np.array(grid1["affine"], dtype=float)
    path.write_bytes(_gzip.compress(nib.Nifti1Image(ct, affine).to_bytes()))
    assert check("ct_mni") is None
    affine[0, 3] += 1e-3  # beyond the 1e-4 mm tolerance
    path.write_bytes(_gzip.compress(nib.Nifti1Image(ct, affine).to_bytes()))
    assert check("ct_mni") == (
        "nifti_grid",
        "Output 'port_a' is not on the MNI152NLin6Asym-res1 grid: its affine differs.",
    )
    path.write_bytes(
        _gzip.compress(
            nib.Nifti1Image(ct[..., None], np.array(grid1["affine"])).to_bytes()
        )
    )
    assert check("ct_mni")[0] == "nifti_grid"  # 4-D is not the 3-D grid


# ------------------------------------------------- real subprocess executor (POSIX)

STEP_SCRIPT = textwrap.dedent(
    """\
    #!{python}
    import json, signal, sys, time
    def stop(*_):
        sys.exit(143)
    signal.signal(signal.SIGTERM, stop)
    marker = {marker!r}
    open(marker, "w").write("started")
    for _ in range(600):
        time.sleep(0.1)
    sys.exit(1)
    """
)


@pytest.mark.skipif(os.name != "posix", reason="subprocess executor needs POSIX")
def test_cancellation_during_a_running_subprocess_step(tmp_path):
    from orchestrator.executors.subprocess import SubprocessExecutor

    marker = tmp_path / "started"
    script = tmp_path / "run-step"
    script.write_text(STEP_SCRIPT.format(python=sys.executable, marker=str(marker)))
    script.chmod(0o755)
    executor = SubprocessExecutor(run_executable=str(script), kill_grace_seconds=5)
    env = Env(tmp_path, executor=executor, config=OrchestratorConfig(poll_seconds=0.2))
    run_id = env.create(env.make_case())

    worker = threading.Thread(
        target=env.orch.drive, args=(run_id, "task-1"), daemon=True
    )
    worker.start()
    deadline = time.monotonic() + 30
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.exists(), "the step never started"
    with env.Session() as db:
        cancel_run(db, run_id, env.user_id)
        db.commit()
    worker.join(timeout=60)
    assert not worker.is_alive()
    db, run = env.run(run_id)
    assert run.status == "canceled"
    db.close()
    # Both independent slots (intake, normalisation) were running; both are stopped.
    attempts = env.attempts(run_id)
    assert attempts and all(a[3:] == ("canceled", "canceled") for a in attempts)
    assert {a[0] for a in attempts} <= {"intake", "normalisation"}
