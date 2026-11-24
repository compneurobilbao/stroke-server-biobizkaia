"""Unit tests of the wrapper logic that need neither FSL nor elastix.

The full registration is exercised by the reference test in CI (container).
"""

import subprocess
import sys
import types
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

STEP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STEP_DIR / "src"))

from ct_lesion_step import step  # noqa: E402
from stroke_step_sdk import InputRejected, InternalFailure, QcFailed  # noqa: E402


def test_age_band_midpoint():
    assert step.age_from_band("65-69") == 67
    assert step.age_from_band("35-39") == 37


@pytest.mark.parametrize("band", ["70-65", "60-130", "abc"])
def test_bad_age_band(band):
    with pytest.raises(InputRejected) as e:
        step.age_from_band(band)
    assert e.value.code == "invalid_parameters"


def test_working_grid_reduction_preserves_volume():
    m = np.zeros((182, 218, 182), bool)
    m[80:100, 100:120, 90:110] = True  # 20 mm cube, aligned with 2 mm voxels
    out = step.to_working_grid(m, 0.5)
    assert out.shape == (91, 109, 91)
    assert abs(int(out.sum()) * 8 - int(m.sum())) <= 0.2 * int(m.sum())
    assert not step.to_working_grid(np.zeros_like(m), 0.5).any()


def _nii(path, data, affine=None):
    nib.save(nib.Nifti1Image(data, np.eye(4) if affine is None else affine), str(path))
    return path


def test_input_checks(tmp_path):
    ct = _nii(tmp_path / "ct.nii.gz", np.zeros((8, 8, 8), np.int16))
    good = np.zeros((8, 8, 8), np.uint8)
    good[2:4, 2:4, 2:4] = 1
    step.check_inputs(ct, _nii(tmp_path / "ok.nii.gz", good))
    with pytest.raises(InputRejected) as e:
        step.check_inputs(ct, _nii(tmp_path / "empty.nii.gz", np.zeros((8, 8, 8), np.uint8)))
    assert e.value.code == "mask_empty"
    with pytest.raises(InputRejected) as e:
        step.check_inputs(ct, _nii(tmp_path / "shape.nii.gz", np.ones((4, 4, 4), np.uint8)))
    assert e.value.code == "mask_grid_mismatch"
    shifted = np.eye(4)
    shifted[0, 3] = 5
    with pytest.raises(InputRejected):
        step.check_inputs(ct, _nii(tmp_path / "aff.nii.gz", good, shifted))
    ct4 = _nii(tmp_path / "ct4.nii.gz", np.zeros((8, 8, 8, 2), np.int16))
    with pytest.raises(InputRejected) as e:
        step.check_inputs(ct4, _nii(tmp_path / "ok2.nii.gz", good))
    assert e.value.code == "ct_not_3d"


def _fake_workflow(monkeypatch, exc):
    mod = types.ModuleType("ct_lesion_to_mni152.registration_workflow")
    mod.call = subprocess.call

    def fail():
        raise exc

    mod.register_ct_and_lesion_to_mni152 = fail
    pkg = types.ModuleType("ct_lesion_to_mni152")
    pkg.registration_workflow = mod
    monkeypatch.setitem(sys.modules, "ct_lesion_to_mni152", pkg)
    monkeypatch.setitem(sys.modules, "ct_lesion_to_mni152.registration_workflow", mod)


def test_failed_fsl_command_is_an_internal_failure(tmp_path, monkeypatch):
    _fake_workflow(monkeypatch, subprocess.CalledProcessError(3, ["/opt/fsl/bin/flirt", "-in", "x"]))
    with pytest.raises(InternalFailure) as e:
        step.run_workflow(tmp_path / "ct.nii.gz", tmp_path / "m.nii.gz", tmp_path)
    assert e.value.details == {"command": "flirt", "exit_code": 3}
    assert "x" not in e.value.message


def test_failed_cluster_with_no_skull_is_qc_failure(tmp_path, monkeypatch):
    _nii(tmp_path / "ct_skull_cluster_sizes.nii.gz", np.zeros((4, 4, 4), np.float32))
    _fake_workflow(monkeypatch, subprocess.CalledProcessError(1, ["fslmaths"]))
    with pytest.raises(QcFailed) as e:
        step.run_workflow(tmp_path / "ct.nii.gz", tmp_path / "m.nii.gz", tmp_path)
    assert e.value.code == "skull_not_found"


def test_wrapper_checks_fsl_exit_codes_without_changing_the_package(tmp_path, monkeypatch):
    seen = {}
    mod = types.ModuleType("ct_lesion_to_mni152.registration_workflow")
    mod.call = subprocess.call

    def register():
        seen["call"] = mod.call

    mod.register_ct_and_lesion_to_mni152 = register
    pkg = types.ModuleType("ct_lesion_to_mni152")
    pkg.registration_workflow = mod
    monkeypatch.setitem(sys.modules, "ct_lesion_to_mni152", pkg)
    monkeypatch.setitem(sys.modules, "ct_lesion_to_mni152.registration_workflow", mod)
    step.run_workflow(tmp_path / "ct.nii.gz", tmp_path / "m.nii.gz", tmp_path)
    assert seen["call"] is subprocess.check_call
    assert mod.call is subprocess.call


def test_package_keeps_its_original_subprocess_call():
    src = (STEP_DIR.parent / "src" / "ct_lesion_to_mni152" / "registration_workflow.py").read_text()
    assert "from subprocess import call\n" in src.replace("\r\n", "\n")


def test_manifest_valid():
    from stroke_step_sdk.manifest import load_manifest

    assert load_manifest(STEP_DIR / "step.yaml").missing_sdk_codes() == []
