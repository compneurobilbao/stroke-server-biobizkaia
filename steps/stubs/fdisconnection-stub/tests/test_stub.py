"""Tests of the functional disconnection STUB on the synthetic fixtures."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import nibabel as nib
import numpy as np

STEP_DIR = Path(__file__).resolve().parents[1]
REPO = STEP_DIR.parents[2]
sys.path.insert(0, str(STEP_DIR / "src"))

from fdisconnection_stub import run as step_run  # noqa: E402

from stroke_step_sdk import EXIT_OK, EXIT_REJECTED, contracts, run_step  # noqa: E402

BUNDLE = REPO / "fixtures/data/bundles/toy-normative-connectome/1.0.0"
MASK = REPO / "fixtures/data/mni_masks/lesion_left_medium.nii.gz"


def execute(tmp_path: Path, mask: Path, seed: int = 3) -> tuple[int, Path]:
    in_dir, out_dir, ref = tmp_path / "in", tmp_path / "out", tmp_path / "ref"
    for d in (in_dir, out_dir, tmp_path / "tmp"):
        d.mkdir()
    shutil.copytree(BUNDLE, ref / "connectome")
    shutil.copyfile(mask, in_dir / "lesion_mask.nii.gz")
    (in_dir / "params.json").write_text(json.dumps({"seed": seed}), encoding="utf-8")
    code = run_step(
        step_run,
        in_dir,
        out_dir,
        manifest_path=STEP_DIR / "step.yaml",
        ref_dir=ref,
        environ={"STEP_CPUS": "1", "TMPDIR": str(tmp_path / "tmp")},
    )
    return code, out_dir


def test_manifest_is_valid_stub():
    issues = contracts.validator().validate_files([STEP_DIR / "step.yaml"])
    assert [i.message for i in issues if i.severity == "error"] == []
    import yaml

    assert yaml.safe_load((STEP_DIR / "step.yaml").read_text())["stub"] is True


def test_map_is_deterministic_and_finite(tmp_path):
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
    code_a, a = execute(tmp_path / "a", MASK)
    code_b, b = execute(tmp_path / "b", MASK)
    assert code_a == code_b == EXIT_OK
    name = "fdisconnection_map.nii.gz"
    va = np.asanyarray(nib.load(str(a / name)).dataobj)
    vb = np.asanyarray(nib.load(str(b / name)).dataobj)
    assert np.array_equal(va, vb) and np.isfinite(va).all() and va.max() > 0
    prov = json.loads((a / "provenance.json").read_text())
    assert [w["code"] for w in prov["warnings"]] == ["stub_output"]
    issues = contracts.validator().validate_files(
        [STEP_DIR / "step.yaml", a / "provenance.json"]
    )
    assert [i.message for i in issues if i.severity == "error"] == []


def test_empty_mask_is_rejected(tmp_path):
    shape = nib.load(str(MASK)).shape
    empty = tmp_path / "empty.nii.gz"
    nib.save(
        nib.Nifti1Image(np.zeros(shape, np.uint8), nib.load(str(MASK)).affine),
        str(empty),
    )
    (tmp_path / "r").mkdir()
    code, out = execute(tmp_path / "r", empty)
    assert code == EXIT_REJECTED
    assert json.loads((out / "error.json").read_text())["code"] == "mask_empty"
