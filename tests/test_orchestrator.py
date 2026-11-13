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
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine, select
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
            target.write_bytes(
                f"{step['name']}:{o['name']}:{sorted(i['sha256'] for i in inputs)}".encode()
            )
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
            for p in out.iterdir()
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
