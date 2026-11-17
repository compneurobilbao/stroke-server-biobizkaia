"""stroke-lnm v0 end to end on fixtures (card F0-33).

The pipeline orchestrator (orchestrator/engine.py) runs the ``ci`` profile of the registry with
the docker executor through the restricted socket proxy, on the synthetic fixtures, with the
bundles registered through orchestrator/bundles.py. The tests under "offline" always run; the
ones under "end to end" are skipped unless the variables below are set (the ``e2e-stroke-lnm``
workflow sets them after building the steps):

``STROKE_TEST_DOCKER_PROXY_URL``     base URL of the socket proxy
``STROKE_LNM_PIPELINES_DIR``         output of ``registry.py materialize`` (contains stroke-lnm/)
``STROKE_LNM_STEP_MANIFESTS_DIR``    step.yaml files read from the built images
``STROKE_LNM_BUNDLES_DIR``           directories of the bundles to register (one per bundle)
``STROKE_LNM_CT_REFERENCE_DIR``      the CT reference case built by ci/ct_reference.sh (ct.nii.gz,
                                     lesion_mask_native.nii.gz, lesion_mask_mni.nii.gz)
``STROKE_LNM_OUT_DIR``               optional: run provenance records are copied here
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "backend"))

_spec = importlib.util.spec_from_file_location("stroke_lnm_registry", HERE / "registry.py")
registry_tool = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = registry_tool
_spec.loader.exec_module(registry_tool)

from contracts import validate as contract  # noqa: E402
from orchestrator.catalog import DirectoryPresetRegistry  # noqa: E402
from orchestrator.planning import PresetRef  # noqa: E402

PROXY = os.environ.get("STROKE_TEST_DOCKER_PROXY_URL")
PIPELINES_DIR = os.environ.get("STROKE_LNM_PIPELINES_DIR")
MANIFESTS_DIR = os.environ.get("STROKE_LNM_STEP_MANIFESTS_DIR")
BUNDLES_DIR = os.environ.get("STROKE_LNM_BUNDLES_DIR")
OUT_DIR = os.environ.get("STROKE_LNM_OUT_DIR")
CT_DIR = os.environ.get("STROKE_LNM_CT_REFERENCE_DIR")
E2E = all((PROXY, PIPELINES_DIR, MANIFESTS_DIR, BUNDLES_DIR, CT_DIR))
needs_e2e = pytest.mark.skipif(not E2E, reason="needs the e2e-stroke-lnm environment")

FIXTURES = REPO / "fixtures" / "data"
PRESET_IDS = ["full", "mni-mask-analysis", "ct-normalisation", "lesion-qc-report"]
SLOTS = [
    "lesion-intake",
    "normalisation",
    "functional-disconnection",
    "tract-disconnection",
    "cognet-profile",
    "report",
]


# -------------------------------------------------------------------------------- offline


def test_e2e_environment_is_complete_when_required():
    """CI sets STROKE_LNM_REQUIRE_E2E: a missing variable must fail, not skip."""
    if os.environ.get("STROKE_LNM_REQUIRE_E2E"):
        assert E2E, "the e2e environment variables are not set"


def test_registry_and_pipeline_agree():
    pipeline, registry = registry_tool.load(HERE)
    assert registry_tool.check(pipeline, registry) == []


def test_committed_pipeline_is_valid_and_lists_every_slot():
    issues = contract.validate_files([HERE / "pipeline.yaml"])
    assert [i.message for i in issues if i.severity == "error"] == []
    from orchestrator.pipeline import load_pipeline

    definition = load_pipeline(HERE / "pipeline.yaml")
    assert [s["name"] for s in definition.slots] == SLOTS
    assert definition.match_input_set(["lesion_mni"]) == "mni-mask"
    assert definition.version == "0.3.0"


def test_report_does_not_need_tract_disconnection():
    """F1-26: the report slot's tract port and the tract slot are optional (QC-only report)."""
    pipeline, _registry = registry_tool.load(HERE)
    slots = {s["name"]: s for s in pipeline["slots"]}
    assert slots["tract-disconnection"]["optional"] is True
    port = next(c for c in slots["report"]["consumes"] if c["port"] == "tract_disconnection")
    assert port["optional"] is True
    assert port["from"] == "tract-disconnection.tract_disconnection"
    # the report step version that states the absence in its section
    for impl in slots["report"]["implementations"]["allowed"]:
        assert impl["step"] == {"name": "research-report", "version": "0.3.0"}


def test_committed_digests_are_placeholders():
    pipeline, registry = registry_tool.load(HERE)
    for impls in registry["implementations"].values():
        for entry in impls.values():
            assert entry["image"].endswith(registry_tool.PLACEHOLDER)


def test_deployment_profile_enables_real_entries_only():
    _pipeline, registry = registry_tool.load(HERE)
    rows = registry_tool.enabled_settings(registry, "deployment")
    enabled = {(r["slot"], r["implementation_id"]) for r in rows if r["enabled"]}
    # the real CT step is mounted and is the default of the normalisation slot
    assert ("normalisation", "ct-lesion-to-mni152") in enabled
    assert registry["implementations"]["normalisation"]["ct-lesion-to-mni152"]["kind"] == "real"
    assert ("functional-disconnection", "flnm-fast") in enabled
    assert ("functional-disconnection", "fdisconnection-stub") not in enabled
    defaults = {r["slot"]: r["implementation_id"] for r in rows if r["is_default"]}
    assert defaults["functional-disconnection"] == "flnm-fast"
    assert defaults["normalisation"] == "ct-lesion-to-mni152"
    ci = registry_tool.enabled_settings(registry, "ci")
    ci_enabled = {(r["slot"], r["implementation_id"]) for r in ci if r["enabled"]}
    # both are enabled in ci; the stub stays the ci default (the real-stack UI specs assert it)
    assert {
        ("functional-disconnection", "flnm-fast"),
        ("functional-disconnection", "fdisconnection-stub"),
    } <= ci_enabled
    ci_defaults = {r["slot"]: r["implementation_id"] for r in ci if r["is_default"]}
    assert ci_defaults["functional-disconnection"] == "fdisconnection-stub"
    assert ci_defaults["normalisation"] == "ct-lesion-to-mni152"
    assert ("normalisation", "ct-lesion-to-mni152") in ci_enabled


def test_ct_step_is_pinned_to_a_commit_of_the_step_repository():
    _pipeline, registry = registry_tool.load(HERE)
    entry = registry["implementations"]["normalisation"]["ct-lesion-to-mni152"]
    assert entry["source"] == {
        "repository": "compneurobilbao/CTLesion2MNI152",
        "commit": "07702382b588f430b52005b61e71a9b48f5fcd15",
        "dockerfile": "step/Dockerfile",
    }


def test_tract_atlas_fixture_has_the_layout_of_the_lnm_step():
    root = FIXTURES / "bundles" / "toy-tract-atlas" / "1.0.0"
    names = (root / "labels.txt").read_text().split()
    assert names and all((root / f"{n}.tck").is_file() for n in names)


# ----------------------------------------------------------------------------- end to end


class _Settings:
    notifications_email_enabled = False
    smtp_host = None
    smtp_from_email = None
    app_public_base_url = "http://localhost"


class Stack:
    """Database, store, bundles and the orchestrator built from the environment."""

    def __init__(self, tmp: Path) -> None:
        from app.db.base import Base
        from app.models import PresetSetting, SlotImplementationSetting, User
        from orchestrator import bundles as reg
        from orchestrator.config import build_orchestrator

        self.tmp = tmp
        self.store_root = tmp / "store"
        engine = create_engine(f"sqlite:///{tmp / 'e2e.db'}")
        Base.metadata.create_all(engine)
        self.Session = sessionmaker(bind=engine, autoflush=False)
        bundle_root = tmp / "bundle-root"
        with self.Session() as db:
            registry = reg.BundleRegistry(bundle_root, reg.SqlAlchemyStore(db))
            for source in sorted(Path(BUNDLES_DIR).iterdir()):
                registry.register(source)
            user = User(email="e2e@example.org", full_name="E2E", role="researcher")
            db.add(user)
            _pipeline, reg_doc = registry_tool.load(Path(PIPELINES_DIR) / "stroke-lnm")
            self.registry = reg_doc
            for row in registry_tool.enabled_settings(reg_doc, "ci"):
                db.add(SlotImplementationSetting(pipeline_id="stroke-lnm", **row))
            for row in registry_tool.enabled_preset_settings(reg_doc, "ci", PRESET_IDS):
                db.add(PresetSetting(pipeline_id="stroke-lnm", **row))
            db.commit()
            self.user_id = user.id
        env = {
            "PIPELINES_DIR": PIPELINES_DIR,
            "STEP_MANIFESTS_DIR": MANIFESTS_DIR,
            "BUNDLE_ROOT": str(bundle_root),
            "ORCHESTRATOR_EXECUTOR": "docker",
            "DOCKER_PROXY_URL": PROXY,
            "ORCHESTRATOR_POLL_SECONDS": "1",
            "ORCHESTRATOR_MAX_ATTEMPTS": "1",
        }
        self.orch = build_orchestrator(
            self.Session, self.store_root, settings=_Settings(), env=env
        )
        self.pipeline = self.orch.pipelines.get("stroke-lnm", "0.3.0")
        assert self.pipeline is not None
        # the presets of the materialized tree (registry.py materialize copies them unchanged)
        self.presets = DirectoryPresetRegistry(PIPELINES_DIR, self.orch.pipelines, self.orch.steps)

    def case(self, files: dict[str, Path]):
        from app.models import Case, InputFile
        from orchestrator.store import sha256_file

        with self.Session() as db:
            # sex and age band are bound to the flnm-fast parameters
            case = Case(
                owner_user_id=self.user_id,
                deployment_profile="research-public",
                sex=CASE_SEX,
                age_band=CASE_AGE_BAND,
            )
            db.add(case)
            db.flush()
            for role, source in files.items():
                rel = f"{case.id}/rawdata/{role}{''.join(source.suffixes)}"
                path = self.store_root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, path)
                db.add(
                    InputFile(
                        case_id=case.id,
                        role=role,
                        artefact_type=self.pipeline.inputs[role],
                        storage_path=rel,
                        sha256=sha256_file(path),
                        size_bytes=path.stat().st_size,
                        uploaded_by_user_id=self.user_id,
                    )
                )
            db.commit()
            return case.id

    def create(self, case_id, **kwargs):
        from app.models import Case, InputFile
        from orchestrator.executors.base import Accounting
        from orchestrator.resolution import create_run

        kwargs.setdefault("presets", self.presets)
        with self.Session() as db:
            inputs = {
                f.role: f
                for f in db.scalars(select(InputFile).where(InputFile.case_id == case_id))
            }
            run = create_run(
                db,
                case=db.get(Case, case_id),
                pipeline=self.pipeline,
                inputs=inputs,
                created_by_user_id=self.user_id,
                steps=self.orch.steps,
                bundles=self.orch.bundles,
                accounting=Accounting(submitted_by=f"user:{self.user_id}"),
                platform_version="0.0.0-ci",
                seed=11,
                **kwargs,
            )
            db.commit()
            return run.id

    def drive(self, run_id):
        from orchestrator.engine import TickResult

        assert self.orch.drive(run_id, "e2e-driver") is TickResult.FINISHED


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    return Stack(tmp_path_factory.mktemp("stroke-lnm-e2e"))


MNI_MASK = FIXTURES / "mni_masks" / "lesion_left_medium.nii.gz"
CASE_SEX, CASE_AGE_BAND = "female", "60-64"


@pytest.fixture(scope="module")
def mni_case(stack):
    return stack.case({"lesion_mni": MNI_MASK})


def _flnm_fast_reference():
    """The synthetic normative bundle's generator and independent reference of the flnm-fast step."""
    path = REPO / "steps" / "flnm-fast" / "tests" / "make_reference.py"
    spec = importlib.util.spec_from_file_location("flnm_fast_make_reference", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def run_record(stack, run_id) -> dict:
    from app.models import PipelineRun
    from orchestrator.provenance import build_run_provenance

    with stack.Session() as db:
        run = db.get(PipelineRun, run_id)
        assert run.status == "succeeded", (run.status, run.failure_code)
        record = build_run_provenance(db, run)
    if OUT_DIR:
        out = Path(OUT_DIR)
        out.mkdir(parents=True, exist_ok=True)
        (out / f"run-provenance-{run_id}.json").write_text(
            json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
        )
    return record


def validate_record(record: dict, tmp: Path) -> None:
    path = tmp / "run-provenance.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    issues = contract.validate_files([path], kind="provenance")
    assert [i.message for i in issues if i.severity == "error"] == []


def artifact_path(stack, run_id, slot, port) -> Path:
    from app.models import Artifact, RunSlot, StepRun

    with stack.Session() as db:
        rel = db.scalar(
            select(Artifact.storage_path)
            .join(StepRun, StepRun.id == Artifact.step_run_id)
            .join(RunSlot, RunSlot.id == StepRun.run_slot_id)
            .where(
                Artifact.pipeline_run_id == run_id,
                RunSlot.slot == slot,
                Artifact.port == port,
            )
        )
    assert rel, f"no artefact {slot}.{port}"
    return stack.store_root / rel


def assert_complete_record(
    stack,
    record: dict,
    fdisconnection: str = "fdisconnection-stub",
    ct_mode: bool = False,
) -> dict[str, dict]:
    _pipeline, registry = registry_tool.load(Path(PIPELINES_DIR) / "stroke-lnm")
    slots = {s["slot"]: s for s in record["slots"]}
    assert list(slots) == SLOTS, "the record lists every slot of the pipeline"
    if not ct_mode:
        assert slots["normalisation"]["status"] == "skipped"
        assert slots["normalisation"]["reason"] == "condition_false"
    for name in SLOTS:
        if name == "normalisation" and not ct_mode:
            continue
        slot = slots[name]
        assert slot["status"] == "selected", slot
        impl = slot["implementation"]
        pinned = registry["implementations"][name][impl["id"]]["image"]
        assert impl["image"] == pinned and "@sha256:" in impl["image"]
        assert not impl["image"].endswith(registry_tool.PLACEHOLDER)
        if name == "functional-disconnection":
            assert impl["id"] == fdisconnection
        assert impl["stub"] is (impl["id"] == "fdisconnection-stub")
    bundles = {
        name: {b["name"]: b for b in slots[name]["bundles"]}
        for name in SLOTS
        if "bundles" in slots[name]
    }
    if fdisconnection == "flnm-fast":
        assert bundles["functional-disconnection"]["normative"]["id"] == "synthetic-normative-fc"
    else:
        assert bundles["functional-disconnection"]["connectome"]["id"] == "toy-normative-connectome"
    assert bundles["tract-disconnection"]["tract_atlas"]["id"] == "toy-tract-atlas"
    assert bundles["cognet-profile"]["concept_maps"]["id"] == "synthetic-concept-maps"
    for per_slot in bundles.values():
        for bundle in per_slot.values():
            assert bundle["content_digest"].startswith("sha256:")
    ran = {s["slot"]: s["execution"]["image"] for s in record["steps"]}
    assert ran == {n: slots[n]["implementation"]["image"] for n in ran}
    assert set(ran) == set(SLOTS) - (set() if ct_mode else {"normalisation"})
    return slots


@needs_e2e
def test_default_run_on_the_mni_mask(stack, mni_case, tmp_path):
    run_id = stack.create(mni_case)
    stack.drive(run_id)
    record = run_record(stack, run_id)
    validate_record(record, tmp_path)
    slots = assert_complete_record(stack, record)
    assert {s["implementation"]["chosen_by"] for n, s in slots.items() if n != "normalisation"} == {
        "deployment_default"
    }
    assert slots["report"]["implementation"]["id"] == "research-report"

    # The tract table of the real step equals the reference computed on the fixture atlas.
    sys.path.insert(0, str(REPO))
    import nibabel as nib
    import numpy as np

    from fixtures import generate as gen

    table = artifact_path(stack, run_id, "tract-disconnection", "tract_disconnection")
    rows = list(csv.DictReader(table.read_text().splitlines(), delimiter="\t"))
    lesion = np.asarray(nib.load(str(MNI_MASK)).dataobj)
    expected = gen.tract_disconnection(
        FIXTURES / "bundles" / "toy-tract-atlas" / "1.0.0", lesion
    )
    assert {r["tract"]: float(r["disconnection"]) for r in rows} == pytest.approx(
        expected, abs=1e-6
    )
    for slot, port in (("report", "report_pdf"), ("report", "report_json")):
        assert artifact_path(stack, run_id, slot, port).stat().st_size > 0
    # the full plan still gives the full report: the tract section is filled in
    report = json.loads(artifact_path(stack, run_id, "report", "report_json").read_text())
    assert report["sections"]["tract_disconnection"] == "available"
    assert report["generator"]["version"] == "0.3.0"


@needs_e2e
def test_run_with_the_real_functional_disconnection_step(stack, mni_case, tmp_path):
    """flnm-fast (the unchanged engine image behind the contract wrapper) on the synthetic normative bundle."""
    run_id = stack.create(
        mni_case, requested_implementations={"functional-disconnection": "flnm-fast"}
    )
    stack.drive(run_id)
    record = run_record(stack, run_id)
    validate_record(record, tmp_path)
    slots = assert_complete_record(stack, record, fdisconnection="flnm-fast")
    chosen = slots["functional-disconnection"]["implementation"]
    assert (chosen["id"], chosen["chosen_by"], chosen["stub"]) == ("flnm-fast", "user", False)

    import nibabel as nib
    import numpy as np

    # The z-map equals the reference computed independently on the synthetic normative bundle:
    # same sex, age band midpoint, the engine default of 1000 controls.
    ref = _flnm_fast_reference()
    shape, _affine = ref.working_grid(REPO / "contracts" / "artefact-types.yaml")
    zmap = artifact_path(stack, run_id, "functional-disconnection", "fdisconnection_map")
    lesion = np.asarray(nib.load(str(MNI_MASK)).dataobj)
    expected_z = ref.expected_zmap(
        ref.parcellation(shape),
        lesion,
        ref.fc_array(),
        ref.subjects(),
        age=62.0,
        sex="F",
        count=1000,
    )
    actual_z = np.asarray(nib.load(str(zmap)).dataobj)
    assert np.allclose(actual_z, expected_z, rtol=1e-5, atol=1e-5)
    assert np.isfinite(actual_z).all() and (actual_z != 0).any()
    fd_step = next(s for s in record["steps"] if s["slot"] == "functional-disconnection")
    assert fd_step["provenance"]["parameters"] == {
        "sex": CASE_SEX,
        "age_band": CASE_AGE_BAND,
        "control_count": 1000,
    }
    assert report_bundle(record) == "synthetic-normative-fc"


def report_bundle(record: dict) -> str:
    slot = next(s for s in record["slots"] if s["slot"] == "functional-disconnection")
    return next(b["id"] for b in slot["bundles"] if b["name"] == "normative")


@needs_e2e
def test_run_with_a_non_default_implementation_in_one_slot(stack, mni_case, tmp_path):
    _pipeline, registry = registry_tool.load(Path(PIPELINES_DIR) / "stroke-lnm")
    alt = registry["profiles"]["ci"]["alternate"]
    run_id = stack.create(
        mni_case, requested_implementations={alt["slot"]: alt["implementation"]}
    )
    stack.drive(run_id)
    record = run_record(stack, run_id)
    validate_record(record, tmp_path)
    slots = assert_complete_record(stack, record)
    chosen = slots[alt["slot"]]["implementation"]
    assert (chosen["id"], chosen["chosen_by"]) == (alt["implementation"], "user")
    report = artifact_path(stack, run_id, "report", "report_json")
    assert report.stat().st_size > 0
    step = next(s for s in record["steps"] if s["slot"] == "report")
    assert step["provenance"]["parameters"]["language"] == "es"


CT_REFERENCE_DICE_FLOOR = 0.40


def _dice(a, b) -> float:
    import numpy as np

    a, b = np.asarray(a) > 0, np.asarray(b) > 0
    return float(2 * (a & b).sum() / (a.sum() + b.sum()))


def _ct_case(stack):
    ref = Path(CT_DIR)
    return stack.case({"ct": ref / "ct.nii.gz", "lesion_native": ref / "lesion_mask_native.nii.gz"})


@needs_e2e
def test_ct_mode_runs_with_the_real_normalisation_step(stack, tmp_path):
    """CT plus native lesion mask: the real ct-lesion-to-mni152 step feeds the rest of the pipeline."""
    import nibabel as nib
    import numpy as np

    ref = Path(CT_DIR)
    run_id = stack.create(_ct_case(stack))
    stack.drive(run_id)
    record = run_record(stack, run_id)
    validate_record(record, tmp_path)
    slots = assert_complete_record(stack, record, ct_mode=True)
    chosen = slots["normalisation"]["implementation"]
    assert (chosen["id"], chosen["chosen_by"], chosen["stub"]) == (
        "ct-lesion-to-mni152",
        "deployment_default",
        False,
    )

    # The step ran with the age band of the case (ventricle template of that band removed).
    step = next(s for s in record["steps"] if s["slot"] == "normalisation")
    assert step["provenance"]["parameters"]["age_band"] == CASE_AGE_BAND

    # Regression floor of the step on the reference case (known lesion shrinkage in its B-spline
    # stage, accepted for now): Dice of the registered mask against the ground truth on the 2 mm
    # grid. Not an accuracy claim.
    mask = nib.load(str(artifact_path(stack, run_id, "normalisation", "lesion_mask_mni")))
    truth = nib.load(str(ref / "lesion_mask_mni.nii.gz"))
    assert mask.shape == truth.shape
    assert np.allclose(mask.affine, truth.affine)
    dice = _dice(mask.dataobj, truth.dataobj)
    print(f"normalisation Dice on the reference case: {dice:.4f}")
    assert dice >= CT_REFERENCE_DICE_FLOOR
    for port in ("ct_mni", "qc_figure"):
        assert artifact_path(stack, run_id, "normalisation", port).stat().st_size > 0

    # Downstream slots consumed the normalised mask: the tract table equals the reference
    # computed on the fixture atlas from the registered lesion.
    sys.path.insert(0, str(REPO))
    from fixtures import generate as gen

    table = artifact_path(stack, run_id, "tract-disconnection", "tract_disconnection")
    rows = list(csv.DictReader(table.read_text().splitlines(), delimiter="\t"))
    expected = gen.tract_disconnection(
        FIXTURES / "bundles" / "toy-tract-atlas" / "1.0.0", np.asarray(mask.dataobj)
    )
    assert {r["tract"]: float(r["disconnection"]) for r in rows} == pytest.approx(
        expected, abs=1e-6
    )
    assert artifact_path(stack, run_id, "report", "report_pdf").stat().st_size > 0


@needs_e2e
def test_ct_mode_is_refused_when_a_deployment_leaves_normalisation_unmounted(stack):
    """A deployment without the CT step still refuses the CT input set (`no_implementation`)."""
    from app.models import SlotImplementationSetting
    from orchestrator.resolution import RunRefused

    case_id = _ct_case(stack)

    def set_enabled(value: bool) -> None:
        with stack.Session() as db:
            for row in db.scalars(
                select(SlotImplementationSetting).where(
                    SlotImplementationSetting.pipeline_id == "stroke-lnm",
                    SlotImplementationSetting.slot == "normalisation",
                )
            ):
                row.enabled = value
                row.is_default = value and row.implementation_id == "ct-lesion-to-mni152"
            db.commit()

    set_enabled(False)
    try:
        with pytest.raises(RunRefused) as refused:
            stack.create(case_id)
    finally:
        set_enabled(True)
    assert refused.value.code == "no_implementation"
    assert refused.value.slot == "normalisation"


@needs_e2e
def test_qc_only_run_when_tract_disconnection_has_no_enabled_implementation(
    stack, mni_case, tmp_path
):
    """F1-26: without an enabled tract implementation the run succeeds with a QC-only report."""
    from app.models import SlotImplementationSetting

    def set_enabled(value: bool) -> None:
        with stack.Session() as db:
            for row in db.scalars(
                select(SlotImplementationSetting).where(
                    SlotImplementationSetting.pipeline_id == "stroke-lnm",
                    SlotImplementationSetting.slot == "tract-disconnection",
                )
            ):
                row.enabled = value
                row.is_default = value and row.implementation_id == "lnm-tracts"
            db.commit()

    set_enabled(False)
    try:
        run_id = stack.create(mni_case)
        stack.drive(run_id)
        record = run_record(stack, run_id)
    finally:
        set_enabled(True)
    validate_record(record, tmp_path)
    slots = {s["slot"]: s for s in record["slots"]}
    assert list(slots) == SLOTS
    assert (slots["tract-disconnection"]["status"], slots["tract-disconnection"]["reason"]) == (
        "unavailable",
        "no_implementation",
    )
    assert "implementation" not in slots["tract-disconnection"]
    for name in ("lesion-intake", "functional-disconnection", "cognet-profile", "report"):
        assert slots[name]["status"] == "selected", name
    ran = {s["slot"] for s in record["steps"]}
    assert ran == {"lesion-intake", "functional-disconnection", "cognet-profile", "report"}

    report = json.loads(artifact_path(stack, run_id, "report", "report_json").read_text())
    assert report["sections"] == {
        "lesion": "available",
        "functional_disconnection": "available",
        "tract_disconnection": "absent",
        "cognitive_profile": "available",
    }
    assert report["tract_disconnection"] == {"status": "absent"}
    tract_slot = next(s for s in report["provenance"]["slots"] if s["slot"] == "tract-disconnection")
    assert tract_slot["status"] == "unavailable"
    assert artifact_path(stack, run_id, "report", "report_pdf").stat().st_size > 0
    step = next(s for s in record["steps"] if s["slot"] == "report")
    assert not [a for a in step["provenance"]["inputs"] if a["port"] == "tract_disconnection"]


# --------------------------------------------------------------------------- presets (F1-20)


def _runs_of(stack, case_id) -> int:
    from app.models import PipelineRun

    with stack.Session() as db:
        return len(db.scalars(select(PipelineRun).where(PipelineRun.case_id == case_id)).all())


@needs_e2e
def test_the_presets_are_attached_to_the_materialized_pipeline(stack):
    attached = stack.presets.presets("stroke-lnm", "0.3.0")
    assert [p.id for p in attached] == sorted(PRESET_IDS)
    for preset in attached:
        raw = (Path(PIPELINES_DIR) / "stroke-lnm" / "presets" / f"{preset.id}.yaml").read_bytes()
        assert preset.sha256 == hashlib.sha256(raw).hexdigest()


@needs_e2e
def test_ct_normalisation_preset_runs_intake_and_normalisation_only(stack, tmp_path):
    """The two slots of the plan succeed, every other slot is `not_requested` and never
    attempted."""
    import nibabel as nib
    import numpy as np

    ref = Path(CT_DIR)
    case_id = _ct_case(stack)
    run_id = stack.create(case_id, preset=PresetRef("ct-normalisation"))
    stack.drive(run_id)
    record = run_record(stack, run_id)
    validate_record(record, tmp_path)

    preset_file = Path(PIPELINES_DIR) / "stroke-lnm" / "presets" / "ct-normalisation.yaml"
    assert record["contract_version"] == "1.2"
    assert record["plan"] == {
        "mode": "preset",
        "preset": {
            "id": "ct-normalisation",
            "version": "1.1.0",
            "sha256": hashlib.sha256(preset_file.read_bytes()).hexdigest(),
        },
        "targets": {"required": ["lesion-intake.lesion_qc", "normalisation.lesion_mask_mni"]},
    }
    slots = {s["slot"]: s for s in record["slots"]}
    assert list(slots) == SLOTS
    for name in ("lesion-intake", "normalisation"):
        assert slots[name]["status"] == "selected", name
        assert slots[name]["plan_role"] == "target"
        assert slots[name]["implementation"]["chosen_by"] == "deployment_default"
    for name in ("functional-disconnection", "tract-disconnection", "cognet-profile", "report"):
        assert (slots[name]["status"], slots[name]["reason"]) == ("skipped", "not_requested"), name
        assert "plan_role" not in slots[name] and "implementation" not in slots[name]
    assert {s["slot"] for s in record["steps"]} == {"lesion-intake", "normalisation"}

    # The normalisation output is the one of the full plan: same floor on the reference case.
    mask = nib.load(str(artifact_path(stack, run_id, "normalisation", "lesion_mask_mni")))
    truth = nib.load(str(ref / "lesion_mask_mni.nii.gz"))
    assert mask.shape == truth.shape
    assert _dice(mask.dataobj, truth.dataobj) >= CT_REFERENCE_DICE_FLOOR
    assert np.isfinite(np.asarray(mask.dataobj)).all()
    assert artifact_path(stack, run_id, "lesion-intake", "lesion_qc").stat().st_size > 0

    from app.models import PipelineRun, RunSlot

    with stack.Session() as db:
        run = db.get(PipelineRun, run_id)
        assert (run.plan_mode, run.preset_id, run.preset_version) == (
            "preset",
            "ct-normalisation",
            "1.1.0",
        )
        rows = {
            r.slot: r for r in db.scalars(select(RunSlot).where(RunSlot.pipeline_run_id == run_id))
        }
        assert {n for n, r in rows.items() if r.reason == "not_requested"} == {
            "functional-disconnection",
            "tract-disconnection",
            "cognet-profile",
            "report",
        }


@needs_e2e
def test_ct_normalisation_preset_is_refused_when_normalisation_is_unmounted(stack):
    from app.models import SlotImplementationSetting
    from orchestrator.resolution import RunRefused

    case_id = _ct_case(stack)

    def set_enabled(value: bool) -> None:
        with stack.Session() as db:
            for row in db.scalars(
                select(SlotImplementationSetting).where(
                    SlotImplementationSetting.pipeline_id == "stroke-lnm",
                    SlotImplementationSetting.slot == "normalisation",
                )
            ):
                row.enabled = value
                row.is_default = value and row.implementation_id == "ct-lesion-to-mni152"
            db.commit()

    set_enabled(False)
    try:
        with pytest.raises(RunRefused) as refused:
            stack.create(case_id, preset=PresetRef("ct-normalisation"))
    finally:
        set_enabled(True)
    assert [(r.code, r.slot) for r in refused.value.reasons] == [
        ("no_implementation", "normalisation")
    ]


def _finished_preset_run(stack, case_id, preset_id, tmp_path):
    """Create, drive and read a preset run that plans the report (card F1-25: they finish)."""
    run_id = stack.create(case_id, preset=PresetRef(preset_id))
    stack.drive(run_id)
    record = run_record(stack, run_id)
    validate_record(record, tmp_path)
    assert artifact_path(stack, run_id, "report", "report_pdf").stat().st_size > 0
    report = json.loads(artifact_path(stack, run_id, "report", "report_json").read_text())
    preset_file = Path(PIPELINES_DIR) / "stroke-lnm" / "presets" / f"{preset_id}.yaml"
    sha256 = hashlib.sha256(preset_file.read_bytes()).hexdigest()
    assert record["contract_version"] == "1.2"
    assert record["plan"]["preset"] == {"id": preset_id, "version": "1.1.0", "sha256": sha256}
    # the report reads the run record as it is (it declares contract 1.2) and states the plan
    assert report["generator"] == {"step": "research-report", "version": "0.3.0"}
    assert report["report_version"] == "1.1"
    assert report["plan"]["mode"] == "preset"
    assert report["plan"]["preset"] == record["plan"]["preset"]
    assert "external_tools" not in report and "declared_origins" not in report
    step = next(s for s in record["steps"] if s["slot"] == "report")
    assert step["provenance"]["contract_version"] == "1.2"
    return record, report


def _ran(record) -> set[str]:
    return {s["slot"] for s in record["steps"]}


@needs_e2e
def test_mni_mask_analysis_preset_finishes_with_the_report(stack, mni_case, tmp_path):
    record, report = _finished_preset_run(stack, mni_case, "mni-mask-analysis", tmp_path)
    slots = {s["slot"]: s for s in record["slots"]}
    assert (slots["normalisation"]["status"], slots["normalisation"]["reason"]) == (
        "skipped",
        "condition_false",
    )
    for name in ("functional-disconnection", "tract-disconnection", "report"):
        assert slots[name]["status"] == "selected" and slots[name]["plan_role"] == "target", name
    assert slots["lesion-intake"]["plan_role"] == "upstream"
    assert slots["cognet-profile"]["plan_role"] == "if_available"
    assert _ran(record) == set(SLOTS) - {"normalisation"}
    assert report["sections"] == {
        "lesion": "available",
        "functional_disconnection": "available",
        "tract_disconnection": "available",
        "cognitive_profile": "available",
    }
    assert {
        "tract-disconnection.tract_disconnection",
        "functional-disconnection.fdisconnection_map",
    } <= set(report["plan"]["targets"]["required"])


@needs_e2e
@pytest.mark.parametrize("input_set", ["mni", "ct"])
def test_full_preset_finishes_with_the_full_report(stack, mni_case, tmp_path, input_set):
    case_id = mni_case if input_set == "mni" else _ct_case(stack)
    record, report = _finished_preset_run(stack, case_id, "full", tmp_path)
    slots = {s["slot"]: s for s in record["slots"]}
    expected = set(SLOTS) if input_set == "ct" else set(SLOTS) - {"normalisation"}
    assert _ran(record) == expected
    assert {n for n, s in slots.items() if s["status"] == "selected"} == expected
    assert report["sections"] == {
        "lesion": "available",
        "functional_disconnection": "available",
        "tract_disconnection": "available",
        "cognitive_profile": "available",
    }
    assert report["plan"]["mode"] == "preset" and report["plan"]["preset"]["id"] == "full"


@needs_e2e
@pytest.mark.parametrize("input_set", ["mni", "ct"])
def test_lesion_qc_report_preset_finishes_with_not_requested_analyses(
    stack, mni_case, tmp_path, input_set
):
    case_id = mni_case if input_set == "mni" else _ct_case(stack)
    record, report = _finished_preset_run(stack, case_id, "lesion-qc-report", tmp_path)
    slots = {s["slot"]: s for s in record["slots"]}
    for name in ("functional-disconnection", "tract-disconnection", "cognet-profile"):
        assert (slots[name]["status"], slots[name]["reason"]) == ("skipped", "not_requested"), name
    # in CT mode the report needs the normalised mask, so the plan adds the normalisation
    expected = {"lesion-intake", "report"} | ({"normalisation"} if input_set == "ct" else set())
    assert _ran(record) == expected
    assert report["sections"] == {
        "lesion": "available",
        "functional_disconnection": "not_requested",
        "tract_disconnection": "not_requested",
        "cognitive_profile": "not_requested",
    }
    for section in ("functional_disconnection", "tract_disconnection", "cognitive_profile"):
        assert report[section] == {"status": "not_requested"}
