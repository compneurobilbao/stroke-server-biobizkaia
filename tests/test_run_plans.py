"""Run plans through run creation, the engine and resume (card F1-19).

Uses the synthetic pipeline and the in-process executor of
``tests/test_orchestrator.py``, extended with a report slot that reads
``run.provenance``. All data is synthetic bytes.
"""

from __future__ import annotations

import copy
import hashlib
import json
import shutil

import pytest
from sqlalchemy import select, update

from app.models import (
    PipelineRun,
    PresetSetting,
    RunSlot,
    SlotImplementationSetting,
    StepRun,
)
from contracts import validate as contract
from orchestrator.actions import resume_run
from orchestrator.catalog import StaticPresetRegistry, load_preset
from orchestrator.engine import TickResult
from orchestrator.pipeline import load_pipeline
from orchestrator.planning import PresetRef, TargetRequest
from orchestrator.provenance import build_run_provenance
from orchestrator.resolution import RunRefused
from tests import test_orchestrator as base

REPORT_IMAGE = (
    "registry.example.org/test/report@sha256:" + hashlib.sha256(b"report").hexdigest()
)
PLAN_YAML = (
    base.PIPELINE_YAML.replace(
        'contract_version: "1.0"', 'contract_version: "1.2"'
    ).replace("id: test-flow", "id: plan-flow")
    + f"""  - name: report
    consumes:
      - {{port: lesion_qc, type: lesion_qc, from: intake.lesion_qc}}
      - {{port: tract_disconnection, type: tract_disconnection,
         from: disconnection.tract_disconnection, optional: true}}
      - {{port: run_provenance, type: run_provenance, from: run.provenance}}
    produces:
      - {{port: report_json, type: report_json}}
    implementations:
      default: report
      allowed:
        - id: report
          step: {{name: report-step, version: 1.0.0}}
          image: {REPORT_IMAGE}
"""
)
PRESET_YAML = b"""\
contract_version: "1.2"
kind: Preset
id: qc-report
version: 1.0.0
title: Lesion QC and report
pipeline: {id: plan-flow, versions: ">=1.0.0, <2.0.0"}
input_sets: [native, mni]
targets:
  required: [report]
"""
UPSTREAM = {
    "name": "example-tool",
    "version": "2.1.0",
    "homepage": "https://example.org/example-tool",
    "image": "registry.example.org/example/tool@sha256:" + "b" * 64,
    "licence": "MIT",
}


def _report_manifest(contract_version: str) -> dict:
    m = base.manifest(
        "report-step",
        "report",
        [
            ("lesion_qc", "lesion_qc", True),
            ("tract_disconnection", "tract_disconnection", False),
            ("run_provenance", "run_provenance", True),
        ],
        [("report_json", "report_json", True)],
        image="report",
    )
    m["contract_version"] = contract_version
    return m


class CapturingExecutor(base.FakeExecutor):
    """Keeps a copy of the staged ``run_provenance`` input of every attempt."""

    def __init__(self, script=None):
        super().__init__(script)
        self.staged_records: list[dict] = []

    def submit(self, invocation):
        handle = super().submit(invocation)
        for name, src in invocation.inputs.items():
            if name.startswith("run_provenance"):
                self.staged_records.append(json.loads(src.read_text()))
        return handle


@pytest.fixture()
def plan_env(tmp_path, monkeypatch):
    def make(report_contract="1.2", script=None, disc_upstream=False):
        manifests = dict(base.MANIFESTS)
        manifests["report-step"] = _report_manifest(report_contract)
        if disc_upstream:
            disc = copy.deepcopy(manifests["disc-step"])
            disc["contract_version"] = "1.2"
            disc["upstream"] = dict(UPSTREAM)
            disc["licence"]["third_party"] = [{"name": "example-tool", "spdx": "MIT"}]
            manifests["disc-step"] = disc
        monkeypatch.setattr(base, "MANIFESTS", manifests)
        monkeypatch.setattr(base, "PIPELINE_YAML", PLAN_YAML)
        monkeypatch.setitem(base.EXT, "report_json", ".json")
        executor = CapturingExecutor(script)
        env = base.Env(tmp_path, executor=executor)
        with env.Session() as db:
            db.execute(
                update(SlotImplementationSetting).values(pipeline_id="plan-flow")
            )
            db.add(
                SlotImplementationSetting(
                    pipeline_id="plan-flow",
                    slot="report",
                    implementation_id="report",
                    enabled=True,
                )
            )
            db.add(
                PresetSetting(
                    pipeline_id="plan-flow", preset_id="qc-report", enabled=True
                )
            )
            db.commit()
        env.presets = StaticPresetRegistry([load_preset(PRESET_YAML)], env.pipelines)
        return env

    return make


def _slots(env, run_id) -> dict[str, tuple]:
    with env.Session() as db:
        return {
            s.slot: (s.status, s.reason, s.plan_role, s.chosen_by)
            for s in db.scalars(
                select(RunSlot).where(RunSlot.pipeline_run_id == run_id)
            )
        }


def _valid(record: dict) -> None:
    assert contract.schema_errors(record, "provenance") == []
    assert contract.check_provenance(record) == []


def _run_file(env, run_id) -> dict:
    db, run = env.run(run_id)
    try:
        return json.loads((env.root / run.provenance_path).read_text())
    finally:
        db.close()


def test_plan_pipeline_is_valid():
    assert load_pipeline(PLAN_YAML.encode()).id == "plan-flow"


def test_full_plan_writes_roles_and_gives_older_steps_the_older_record(plan_env):
    env = plan_env(report_contract="1.0")
    run_id = env.create(env.make_case())
    db, run = env.run(run_id)
    assert (run.plan_mode, run.preset_id, run.targets) == ("full", None, {})
    db.close()
    assert _slots(env, run_id) == {
        "intake": ("selected", None, "target", "pipeline_default"),
        "normalisation": ("selected", None, "target", "pipeline_default"),
        "disconnection": ("selected", None, "target", "pipeline_default"),
        "report": ("selected", None, "target", "pipeline_default"),
    }
    assert env.drive(run_id) is TickResult.FINISHED
    record = _run_file(env, run_id)
    assert record["status"] == "succeeded"
    assert record["contract_version"] == "1.2"
    assert record["plan"] == {"mode": "full", "preset": None, "targets": {}}
    _valid(record)
    # The report step declares contract 1.0: it got the record without the plan.
    (staged,) = env.executor.staged_records
    assert staged["contract_version"] == "1.0" and "plan" not in staged
    assert not any("plan_role" in s for s in staged["slots"])
    _valid(staged)


def test_free_plan_runs_only_the_planned_slots(plan_env):
    env = plan_env()
    run_id = env.create(
        env.make_case(), targets=TargetRequest(("tract_disconnection",))
    )
    db, run = env.run(run_id)
    assert run.plan_mode == "free"
    assert run.targets == {"required": ["disconnection.tract_disconnection"]}
    db.close()
    assert _slots(env, run_id) == {
        "intake": ("skipped", "not_requested", None, None),
        "normalisation": ("selected", None, "upstream", "pipeline_default"),
        "disconnection": ("selected", None, "target", "pipeline_default"),
        "report": ("skipped", "not_requested", None, None),
    }
    assert env.drive(run_id) is TickResult.FINISHED
    db, run = env.run(run_id)
    assert run.status == "succeeded"
    db.close()
    # No attempt for a slot outside the plan.
    assert [a[0] for a in env.attempts(run_id)] == ["normalisation", "disconnection"]
    record = _run_file(env, run_id)
    assert record["plan"]["mode"] == "free"
    _valid(record)
    with env.Session() as db:
        regenerated = build_run_provenance(db, db.get(PipelineRun, run_id))
    _valid(regenerated)
    assert regenerated["slots"] == record["slots"]


def test_preset_run_and_resume_after_the_preset_is_disabled(plan_env):
    env = plan_env(script={"report-step": ["fail", "fail"]})
    run_id = env.create(
        env.make_case(), preset=PresetRef("qc-report"), presets=env.presets
    )
    db, run = env.run(run_id)
    assert (run.plan_mode, run.preset_id, run.preset_version) == (
        "preset",
        "qc-report",
        "1.0.0",
    )
    assert run.preset_sha256 == hashlib.sha256(PRESET_YAML).hexdigest()
    assert run.targets == {"required": ["report.report_json"]}
    db.close()
    assert _slots(env, run_id) == {
        "intake": ("selected", None, "upstream", "pipeline_default"),
        "normalisation": ("skipped", "not_requested", None, None),
        "disconnection": ("skipped", "not_requested", None, None),
        "report": ("selected", None, "target", "pipeline_default"),
    }
    assert env.drive(run_id) is TickResult.FINISHED
    db, run = env.run(run_id)
    assert run.status == "failed"
    db.close()
    # Withdrawing the preset hides it for new runs only.
    with env.Session() as db:
        db.execute(update(PresetSetting).values(enabled=False))
        db.commit()
    with pytest.raises(RunRefused) as exc:
        env.create(env.make_case(), preset=PresetRef("qc-report"), presets=env.presets)
    assert exc.value.code == "preset_not_enabled"
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
    assert result.enqueue
    assert env.drive(run_id) is TickResult.FINISHED
    db, run = env.run(run_id)
    assert run.status == "succeeded"
    db.close()
    assert [a[0] for a in env.attempts(run_id)] == [
        "intake",
        "report",
        "report",
        "report",
    ]
    record = _run_file(env, run_id)
    assert record["plan"]["preset"]["id"] == "qc-report"
    _valid(record)
    # The report declares contract 1.2: it got the plan and the not_requested slots.
    staged = env.executor.staged_records[-1]
    assert staged["plan"]["mode"] == "preset"
    assert {s["slot"]: s.get("reason") for s in staged["slots"]}["disconnection"] == (
        "not_requested"
    )
    _valid(staged)


def test_a_plan_older_steps_cannot_read_is_refused(plan_env):
    env = plan_env(report_contract="1.0")
    with pytest.raises(RunRefused) as exc:
        env.create(env.make_case(), preset=PresetRef("qc-report"), presets=env.presets)
    assert (exc.value.code, exc.value.slot) == ("step_contract_too_old", "report")
    with env.Session() as db:
        assert db.scalar(select(PipelineRun.id)) is None


def test_refusals_carry_every_reason_and_write_nothing(plan_env):
    env = plan_env()
    case_id = env.make_case(roles=("lesion_mni",))
    with pytest.raises(RunRefused) as exc:
        env.create(case_id, targets=TargetRequest(("no-such-thing", "normalisation")))
    assert [r.code for r in exc.value.reasons] == [
        "unknown_target",
        "target_not_applicable",
    ]
    assert exc.value.code == "unknown_target"
    with pytest.raises(RunRefused) as exc:
        env.create(
            case_id,
            preset=PresetRef("qc-report"),
            targets=TargetRequest(("report",)),
            presets=env.presets,
        )
    assert exc.value.code == "preset_and_targets"
    with pytest.raises(RunRefused) as exc:
        env.create(case_id, preset=PresetRef("missing"), presets=env.presets)
    assert exc.value.code == "preset_not_found"
    with env.Session() as db:
        assert db.scalar(select(PipelineRun.id)) is None


def test_full_plan_keeps_the_previous_refusal_order(plan_env, monkeypatch):
    """A missing manifest of an earlier slot is reported before a later slot without an
    implementation, as run creation did before plans."""
    env = plan_env()
    with env.Session() as db:
        db.execute(
            update(SlotImplementationSetting)
            .where(SlotImplementationSetting.slot == "disconnection")
            .values(enabled=False)
        )
        db.commit()
    case_id = env.make_case()
    with pytest.raises(RunRefused) as exc:
        env.create(case_id)
    assert (exc.value.code, exc.value.slot) == ("no_implementation", "disconnection")
    monkeypatch.setattr(env.steps, "_items", {})
    with pytest.raises(RunRefused) as exc:
        env.create(case_id)
    assert (exc.value.code, exc.value.slot) == ("manifest_unavailable", "intake")


def test_upstream_block_is_copied_into_the_slot_and_the_record(plan_env):
    env = plan_env(disc_upstream=True)
    run_id = env.create(env.make_case())
    with env.Session() as db:
        slot = db.scalar(
            select(RunSlot).where(
                RunSlot.pipeline_run_id == run_id, RunSlot.slot == "disconnection"
            )
        )
        assert slot.upstream == UPSTREAM
        record = build_run_provenance(db, db.get(PipelineRun, run_id))
    disc = next(s for s in record["slots"] if s["slot"] == "disconnection")
    assert disc["implementation"]["upstream"] == UPSTREAM
    _valid(record)


def test_runs_without_plan_roles_are_regenerated_without_a_plan(plan_env):
    """data-model.md 8: runs written between migration 0013 and F1-19 have no roles."""
    env = plan_env()
    run_id = env.create(env.make_case(roles=("lesion_mni",)))
    with env.Session() as db:
        db.execute(
            update(RunSlot)
            .where(RunSlot.pipeline_run_id == run_id)
            .values(plan_role=None)
        )
        db.commit()
        record = build_run_provenance(db, db.get(PipelineRun, run_id))
    assert record["contract_version"] == "1.1" and "plan" not in record
    assert not any("plan_role" in s for s in record["slots"])
    _valid(record)


def test_step_runs_are_never_created_for_skipped_slots(plan_env):
    env = plan_env()
    run_id = env.create(env.make_case(), targets=TargetRequest(("intake",)))
    assert env.drive(run_id) is TickResult.FINISHED
    with env.Session() as db:
        names = db.scalars(
            select(RunSlot.slot)
            .join(StepRun, StepRun.run_slot_id == RunSlot.id)
            .where(RunSlot.pipeline_run_id == run_id)
        ).all()
        assert db.get(PipelineRun, run_id).status == "succeeded"
    assert names == ["intake"]
    shutil.rmtree(env.root, ignore_errors=True)


def test_a_free_plan_with_an_older_report_is_refused(plan_env):
    """Review fix: any preset or free plan with a selected step that reads
    run.provenance and declares a contract older than 1.2, even without slots
    outside the plan."""
    env = plan_env(report_contract="1.0")
    with pytest.raises(RunRefused) as exc:
        env.create(
            env.make_case(),
            targets=TargetRequest(("report", "disconnection"), ("intake",)),
        )
    assert (exc.value.code, exc.value.slot) == ("step_contract_too_old", "report")


def test_slot_rows_of_a_plan_run_are_valid_api_reads(plan_env):
    """GET /runs/{id} reads run_slots through RunSlotRead: not_requested, plan_role,
    chosen_by preset and upstream must validate."""
    from app.schemas.run import RunSlotRead

    env = plan_env(disc_upstream=True)
    preset_run = env.create(
        env.make_case(), preset=PresetRef("qc-report"), presets=env.presets
    )
    with env.Session() as db:
        db.execute(
            update(PipelineRun).values(
                status="canceled", finished_at=PipelineRun.created_at
            )
        )
        db.execute(
            update(RunSlot)
            .where(RunSlot.pipeline_run_id == preset_run, RunSlot.slot == "intake")
            .values(chosen_by="preset")
        )
        db.commit()
    free_run = env.create(
        env.make_case(), targets=TargetRequest(("tract_disconnection",))
    )
    with env.Session() as db:
        for run_id in (preset_run, free_run):
            rows = db.scalars(
                select(RunSlot).where(RunSlot.pipeline_run_id == run_id)
            ).all()
            reads = {r.slot: RunSlotRead.model_validate(r) for r in rows}
            assert any(r.reason == "not_requested" for r in reads.values())
            assert all(
                (r.plan_role is None)
                == (r.reason in ("not_requested", "condition_false"))
                for r in reads.values()
            )
        assert reads["disconnection"].upstream["name"] == "example-tool"
