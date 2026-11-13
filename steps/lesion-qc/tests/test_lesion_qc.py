"""Tests of the lesion-qc step: fixtures, every failure mode, schema and contract checks.

Everything runs in-process; container paths are exercised by .github/workflows/steps.yml.
"""

from __future__ import annotations

import gzip
import json
import shutil
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from jsonschema import Draft202012Validator

STEP_DIR = Path(__file__).resolve().parents[1]
REPO = STEP_DIR.parents[1]
FIXTURES = REPO / "fixtures" / "data"
sys.path.insert(0, str(STEP_DIR / "src"))

import importlib.util  # noqa: E402

# loaded under a unique name: other steps also have a tests/make_reference.py
_spec = importlib.util.spec_from_file_location(
    "lesion_qc_make_reference", STEP_DIR / "tests" / "make_reference.py"
)
make_reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_reference)
from lesion_qc_step import run as step_run  # noqa: E402

from stroke_step_sdk import (  # noqa: E402
    EXIT_OK,
    EXIT_REJECTED,
    contracts,
    reftest,
    run_step,
)

REGISTRY = contracts.contracts_dir() / "artefact-types.yaml"
MANIFEST = STEP_DIR / "step.yaml"
SCHEMA = Draft202012Validator(
    json.loads((STEP_DIR / "lesion_qc.schema.json").read_text(encoding="utf-8"))
)
SHAPE, AFFINE = make_reference.working_grid(REGISTRY)
PORT_FILES = {
    "lesion_mni": "lesion_mni.nii.gz",
    "lesion_native": "lesion_native.nii.gz",
    "ct": "ct.nii.gz",
}


class Staged:
    def __init__(self, root: Path) -> None:
        self.in_dir, self.out_dir = root / "in", root / "out"
        for d in (self.in_dir, self.out_dir, root / "tmp"):
            d.mkdir()
        self.env = {"STEP_CPUS": "1", "TMPDIR": str(root / "tmp")}
        self.params: dict = {}

    def put(self, port: str, data=None, affine=AFFINE, code: int = 4, copy: Path | None = None):
        dest = self.in_dir / PORT_FILES[port]
        if copy is not None:
            shutil.copyfile(copy, dest)
            return
        img = nib.Nifti1Image(data, affine)
        img.set_sform(affine, code=code)
        img.set_qform(affine, code=code)
        nib.save(img, str(dest))

    def run(self) -> int:
        (self.in_dir / "params.json").write_text(json.dumps(self.params), encoding="utf-8")
        return run_step(
            step_run, self.in_dir, self.out_dir, manifest_path=MANIFEST, environ=self.env
        )

    def json(self, name: str) -> dict:
        return json.loads((self.out_dir / name).read_text(encoding="utf-8"))

    def qc(self) -> dict:
        doc = self.json("lesion_qc.json")
        assert list(SCHEMA.iter_errors(doc)) == []
        return doc

    def error_code(self) -> str:
        err = self.json("error.json")
        assert err["message"]
        return err["code"]

    def validate(self) -> list[str]:
        docs = [MANIFEST, self.out_dir / "provenance.json"]
        if (self.out_dir / "error.json").exists():
            docs.append(self.out_dir / "error.json")
        issues = contracts.validator().validate_files(docs)
        return [i.message for i in issues if i.severity == "error"]


@pytest.fixture
def staged(tmp_path) -> Staged:
    return Staged(tmp_path)


def ball(centre=(60, 60, 40), radius=6.0):
    return make_reference.ball(SHAPE, centre, radius)


def rejected(staged: Staged, code: str, category: str = "input_rejected") -> None:
    assert staged.run() == EXIT_REJECTED
    err = staged.json("error.json")
    assert (err["category"], err["code"]) == (category, code)
    assert err["message"]
    assert staged.validate() == []
    assert not (staged.out_dir / "lesion_qc.json").exists()


# --- manifest, reference dataset, schema ------------------------------------------------


def test_manifest_declares_standard_codes():
    from stroke_step_sdk.manifest import load_manifest

    assert load_manifest(MANIFEST).missing_sdk_codes() == []


def test_reference_dataset_local_run(tmp_path):
    make_reference.generate(STEP_DIR / "tests" / "reference", REGISTRY)
    prov = tmp_path / "provenance.json"
    code = reftest.run_reference_test(
        STEP_DIR, "local", workdir=tmp_path / "work", save_provenance=prov
    )
    assert code == 0
    record = json.loads(prov.read_text(encoding="utf-8"))
    assert record["status"] == "succeeded"
    assert {o["port"] for o in record["outputs"]} == {
        "lesion_qc",
        "lesion_mask_mni",
        "qc_figure",
    }


def test_schema_is_a_valid_json_schema():
    Draft202012Validator.check_schema(SCHEMA.schema)


# --- MNI mode on the committed fixtures ---------------------------------------------------


def _index():
    rows = (FIXTURES / "mni_masks" / "index.tsv").read_text().strip().splitlines()[1:]
    return [r.split("\t") for r in rows]


@pytest.mark.parametrize("name,side,volume,n_vox", _index())
def test_mni_fixture_masks(staged, name, side, volume, n_vox):
    staged.put("lesion_mni", copy=FIXTURES / "mni_masks" / f"{name}.nii.gz")
    assert staged.run() == EXIT_OK
    qc = staged.qc()
    assert qc["mode"] == "mni_mask" and qc["grid"]["resampled"] is False
    assert qc["n_voxels"] == int(n_vox)
    assert qc["volume_ml"] == pytest.approx(float(volume), abs=1e-3)
    assert qc["hemisphere"]["label"] == side
    codes = [w["code"] for w in qc["warnings"]]
    # the two-part blob fixture overlaps, so it is a single connected component
    assert qc["components"]["count"] == 1
    assert "multiple_components" not in codes
    assert ("small_lesion" in codes) == (float(volume) < 0.5)
    # the output mask equals the input on the working grid
    out = nib.load(str(staged.out_dir / "lesion_mask_mni.nii.gz"))
    assert out.shape == tuple(SHAPE)
    assert int(np.asanyarray(out.dataobj).sum()) == int(n_vox)
    png = (staged.out_dir / "qc_figure.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 5000
    assert staged.validate() == []


def test_one_part_per_component_volumes(staged):
    data = ball((30, 60, 40), 4.0) | ball((60, 60, 40), 3.0)
    staged.put("lesion_mni", data)
    assert staged.run() == EXIT_OK
    qc = staged.qc()
    assert qc["components"]["count"] == 2
    assert qc["components"]["volumes_ml"][0] > qc["components"]["volumes_ml"][1]
    assert [w["code"] for w in qc["warnings"]] == ["multiple_components", "bilateral_lesion"]
    # right (x>0) and left parts: voxel 30 is x=+30 and voxel 60 is x=-30
    assert qc["hemisphere"]["label"] == "bilateral"


# --- CT mode ---------------------------------------------------------------------------


def test_ct_mode_on_native_mask(staged):
    case = FIXTURES / "cases" / "case-01"
    staged.put("lesion_native", copy=case / "lesion_mask_native.nii.gz")
    staged.put("ct", copy=case / "ct.nii.gz")
    assert staged.run() == EXIT_OK
    qc = staged.qc()
    truth = json.loads((case / "truth.json").read_text())["lesion"]
    assert qc["mode"] == "ct_native" and qc["input_space"] == "native"
    assert qc["grid"]["name"] is None and qc["grid"]["resampled"] is False
    assert qc["grid"]["shape"] == [76, 92, 60]
    assert qc["volume_ml"] == pytest.approx(truth["volume_ml_native"], abs=1e-3)
    assert qc["hemisphere"]["label"] == "not_assessed"
    assert not (staged.out_dir / "lesion_mask_mni.nii.gz").exists()
    assert (staged.out_dir / "qc_figure.png").read_bytes()[:4] == b"\x89PNG"
    assert staged.validate() == []


def test_ct_mode_without_ct(staged):
    staged.put("lesion_native", copy=FIXTURES / "cases" / "case-02" / "lesion_mask_native.nii.gz")
    assert staged.run() == EXIT_OK
    assert staged.qc()["mode"] == "ct_native"


def test_ct_grid_mismatch(staged):
    case = FIXTURES / "cases" / "case-01"
    staged.put("lesion_native", copy=case / "lesion_mask_native.nii.gz")
    staged.put("ct", np.zeros((76, 92, 59), np.int16), np.diag([2.5, 2.5, 3, 1.0]), code=1)
    rejected(staged, "ct_grid_mismatch")


# --- failure modes ----------------------------------------------------------------------


def test_not_a_nifti(staged):
    (staged.in_dir / PORT_FILES["lesion_mni"]).write_bytes(gzip.compress(b"hello, not an image"))
    rejected(staged, "input_format_invalid")
    assert staged.json("error.json")["port"] == "lesion_mni"


def test_not_gzip_at_all(staged):
    (staged.in_dir / PORT_FILES["lesion_mni"]).write_bytes(b"plain text")
    rejected(staged, "input_format_invalid")


def test_non_binary_mask(staged):
    data = np.zeros(SHAPE, np.float32)
    data[40:44, 40:44, 40:44] = 0.5
    staged.put("lesion_mni", data)
    rejected(staged, "input_format_invalid")


def test_non_binary_native_mask(staged):
    case = FIXTURES / "cases" / "case-01"
    img = nib.load(str(case / "lesion_mask_native.nii.gz"))
    data = np.asanyarray(img.dataobj).astype(np.int16) * 7
    nib.save(nib.Nifti1Image(data, img.affine), str(staged.in_dir / PORT_FILES["lesion_native"]))
    rejected(staged, "input_format_invalid")


def test_empty_mask(staged):
    staged.put("lesion_mni", np.zeros(SHAPE, np.uint8))
    rejected(staged, "mask_empty")


def test_empty_native_mask(staged):
    staged.put("lesion_native", np.zeros((20, 20, 20), np.uint8), np.eye(4), code=1)
    rejected(staged, "mask_empty")


def test_wrong_grid_native_space_declared(staged):
    """A mask drawn on a CT (scanner coordinates) given as an MNI mask."""
    staged.put("lesion_mni", copy=FIXTURES / "cases" / "case-01" / "lesion_mask_native.nii.gz")
    rejected(staged, "space_mismatch")


def test_wrong_grid_space_not_declared(staged):
    staged.put("lesion_mni", ball(), code=0)
    rejected(staged, "space_not_declared")


def test_wrong_grid_oblique_axes(staged):
    theta = np.deg2rad(30)
    rot = np.eye(4)
    rot[:2, :2] = [[np.cos(theta), -np.sin(theta)], [np.sin(theta), np.cos(theta)]]
    staged.put("lesion_mni", ball(), AFFINE @ rot)
    rejected(staged, "space_mismatch")


def test_wrong_grid_outside_template_extent(staged):
    """A 1 mm mask whose world coordinates are far outside the MNI box (voxel-index origin)."""
    data = np.zeros((60, 60, 60), np.uint8)
    data[10:40, 10:40, 10:40] = 1
    staged.put("lesion_mni", data, np.diag([-1.0, 1, 1, 1]) + np.array([[0, 0, 0, 400], [0, 0, 0, 400], [0, 0, 0, 400], [0, 0, 0, 0.0]]))
    rejected(staged, "space_mismatch")


def test_aligned_code_is_accepted_with_a_warning(staged):
    staged.put("lesion_mni", ball(), code=2)
    assert staged.run() == EXIT_OK
    provenance = staged.json("provenance.json")
    assert "space_code_not_mni152" in [w["code"] for w in provenance["warnings"]]


def test_four_dimensional_mask(staged):
    data = np.zeros(SHAPE + (2,), np.uint8)
    data[40:44, 40:44, 40:44, :] = 1
    img = nib.Nifti1Image(data, AFFINE)
    img.set_sform(AFFINE, code=4)
    nib.save(img, str(staged.in_dir / PORT_FILES["lesion_mni"]))
    rejected(staged, "mask_not_3d")


def test_conflicting_inputs(staged):
    staged.put("lesion_mni", ball())
    staged.put("lesion_native", copy=FIXTURES / "cases" / "case-01" / "lesion_mask_native.nii.gz")
    rejected(staged, "conflicting_inputs")


def test_no_lesion_input(staged):
    rejected(staged, "lesion_input_missing")


def test_ct_without_mask(staged):
    staged.put("ct", copy=FIXTURES / "cases" / "case-01" / "ct.nii.gz")
    rejected(staged, "lesion_input_missing")


def test_unknown_parameter(staged):
    staged.put("lesion_mni", ball())
    staged.params = {"nope": 1}
    assert staged.run() == EXIT_REJECTED
    assert staged.error_code() == "invalid_parameters"


def test_lesion_too_large(staged):
    staged.put("lesion_mni", ball((45, 54, 40), 30.0))
    staged.params = {"max_volume_ml": 50}
    rejected(staged, "lesion_too_large", "qc_failed")
    assert staged.json("error.json")["details"]["max_volume_ml"] == 50


# --- hemisphere handling ------------------------------------------------------------------


def test_both_hemispheres_is_a_warning_by_default(staged):
    staged.put("lesion_mni", ball((45, 60, 40), 8.0))  # straddles x = 0
    assert staged.run() == EXIT_OK
    qc = staged.qc()
    assert qc["hemisphere"]["label"] == "bilateral"
    assert qc["hemisphere"]["left_voxels"] > 0 and qc["hemisphere"]["right_voxels"] > 0
    assert "bilateral_lesion" in [w["code"] for w in qc["warnings"]]


def test_both_hemispheres_can_be_rejected(staged):
    staged.put("lesion_mni", ball((45, 60, 40), 8.0))
    staged.params = {"reject_bilateral": True}
    rejected(staged, "bilateral_lesion", "qc_failed")


def test_midline_spill_stays_unilateral(staged):
    data = ball((60, 60, 40), 6.0)
    data[44, 60, 40] = 1  # one voxel at x = +2
    data[45, 60, 40] = 1  # and one at x = 0 (midline)
    staged.put("lesion_mni", data)
    assert staged.run() == EXIT_OK
    h = staged.qc()["hemisphere"]
    assert h["label"] == "left" and h["right_voxels"] == 1 and h["midline_voxels"] == 1


def test_right_side_sign(staged):
    staged.put("lesion_mni", ball((30, 60, 40), 5.0))
    assert staged.run() == EXIT_OK
    assert staged.qc()["hemisphere"]["label"] == "right"


# --- resampling -----------------------------------------------------------------------------


def _res1_affine():
    return np.array(
        [[-1, 0, 0, 90], [0, 1, 0, -126], [0, 0, 1, -72], [0, 0, 0, 1.0]]
    )


def test_one_mm_mask_is_resampled(staged):
    data = np.zeros((182, 218, 182), np.uint8)
    data[100:130, 100:130, 80:110] = 1  # 30 mm cube, x = -10 .. -39
    staged.put("lesion_mni", data, _res1_affine())
    assert staged.run() == EXIT_OK
    qc = staged.qc()
    assert qc["grid"]["resampled"] is True and qc["grid"]["shape"] == [91, 109, 91]
    assert qc["grid"]["source_shape"] == [182, 218, 182]
    assert qc["volume_source_ml"] == pytest.approx(27.0)
    assert qc["volume_ml"] == pytest.approx(27.0, rel=0.15)
    assert qc["hemisphere"]["label"] == "left"
    out = nib.load(str(staged.out_dir / "lesion_mask_mni.nii.gz"))
    assert out.shape == tuple(SHAPE) and np.allclose(out.affine, AFFINE)
    assert staged.validate() == []


def test_flipped_axis_orientation_is_resampled(staged):
    """The same lesion stored with a flipped x axis lands on the same voxels."""
    ref = ball((60, 60, 40), 6.0)
    flipped = ref[::-1]
    aff = AFFINE.copy()
    aff[0, 0], aff[0, 3] = 2.0, -90.0
    staged.put("lesion_mni", flipped, aff)
    assert staged.run() == EXIT_OK
    qc = staged.qc()
    assert qc["grid"]["resampled"] is True
    assert qc["n_voxels"] == int(ref.sum())
    assert qc["hemisphere"]["label"] == "left"


def test_tiny_lesion_lost_by_resampling(staged):
    data = np.zeros((182, 218, 182), np.uint8)
    data[101, 101, 81] = 1  # odd indices: no 2 mm voxel centre coincides
    staged.put("lesion_mni", data, _res1_affine())
    rejected(staged, "mask_empty_after_resampling")


# --- determinism and contract -------------------------------------------------------------


def test_outputs_are_deterministic(tmp_path):
    results = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        s = Staged(tmp_path / name)
        s.put("lesion_mni", copy=FIXTURES / "mni_masks" / "lesion_left_medium.nii.gz")
        assert s.run() == EXIT_OK
        results.append(
            [(s.out_dir / f).read_bytes()
             for f in ("lesion_qc.json", "lesion_mask_mni.nii.gz", "qc_figure.png")]
        )
    assert results[0] == results[1]
