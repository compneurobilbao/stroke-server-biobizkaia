"""Tests of the template step: reference dataset, error paths, contract validation.

Everything runs in-process or through ``bin/run`` locally; the container paths (Docker,
Apptainer) are exercised by .github/workflows/steps.yml with ``stroke-step-reftest``.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

STEP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STEP_DIR / "src"))
sys.path.insert(0, str(STEP_DIR / "tests"))

import make_reference  # noqa: E402
from template_step import run as step_run  # noqa: E402

from stroke_step_sdk import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_REJECTED,
    contracts,
    reftest,
    run_step,
)  # noqa: E402

REGISTRY = contracts.contracts_dir() / "artefact-types.yaml"
MANIFEST = STEP_DIR / "step.yaml"


@pytest.fixture(scope="module")
def reference_dir() -> Path:
    """The reference dataset the manifest points to, generated here (not committed)."""
    dest = STEP_DIR / "tests" / "reference"
    make_reference.generate(dest, REGISTRY)
    return dest


class Staged:
    def __init__(self, root: Path) -> None:
        self.in_dir, self.out_dir = root / "in", root / "out"
        for d in (self.in_dir, self.out_dir, root / "tmp"):
            d.mkdir()
        (self.in_dir / "params.json").write_text("{}", encoding="utf-8")
        self.env = {"STEP_CPUS": "1", "TMPDIR": str(root / "tmp")}

    def mask(self, values: np.ndarray) -> None:
        shape, affine = make_reference.working_grid(REGISTRY)
        assert values.shape == shape
        nib.save(
            nib.Nifti1Image(values, affine), str(self.in_dir / "lesion_mask.nii.gz")
        )

    def run(self) -> int:
        return run_step(
            step_run,
            self.in_dir,
            self.out_dir,
            manifest_path=MANIFEST,
            environ=self.env,
        )

    def json(self, name: str) -> dict:
        return json.loads((self.out_dir / name).read_text(encoding="utf-8"))

    def validate(self) -> list[str]:
        docs = [MANIFEST, self.out_dir / "provenance.json"]
        if (self.out_dir / "error.json").exists():
            docs.append(self.out_dir / "error.json")
        issues = contracts.validator().validate_files(docs)
        return [i.message for i in issues if i.severity == "error"]


@pytest.fixture
def staged(tmp_path) -> Staged:
    return Staged(tmp_path)


def test_manifest_is_valid_and_declares_standard_codes():
    from stroke_step_sdk.manifest import load_manifest

    manifest = load_manifest(MANIFEST)
    assert manifest.missing_sdk_codes() == []


def test_reference_dataset_local_run(reference_dir, tmp_path):
    """The manifest's reference_test, run with bin/run, checked like the orchestrator would."""
    prov = tmp_path / "provenance.json"
    code = reftest.run_reference_test(
        STEP_DIR, "local", workdir=tmp_path / "work", save_provenance=prov
    )
    assert code == 0
    record = json.loads(prov.read_text(encoding="utf-8"))
    assert record["status"] == "succeeded"
    assert [o["port"] for o in record["outputs"]] == ["lesion_qc"]


def test_expected_volume_of_the_ball(reference_dir, staged):
    shutil.copyfile(
        reference_dir / "lesion_mask.nii.gz", staged.in_dir / "lesion_mask.nii.gz"
    )
    assert staged.run() == EXIT_OK
    result = staged.json("lesion_qc.json")
    expected = json.loads((reference_dir / "expected" / "lesion_qc.json").read_text())
    assert result["n_voxels"] == expected["n_voxels"] == 925
    assert result["volume_ml"] == pytest.approx(7.4, rel=1e-6)
    assert staged.validate() == []


def test_empty_mask_is_rejected(staged):
    shape, _ = make_reference.working_grid(REGISTRY)
    staged.mask(np.zeros(shape, np.uint8))
    assert staged.run() == EXIT_REJECTED
    err = staged.json("error.json")
    assert (err["category"], err["code"], err["port"]) == (
        "input_rejected",
        "mask_empty",
        "lesion_mask",
    )
    assert staged.validate() == []


def test_non_binary_mask_is_rejected_by_the_sdk(staged):
    shape, _ = make_reference.working_grid(REGISTRY)
    values = np.zeros(shape, np.float32)
    values[10:12, 10:12, 10:12] = 255
    staged.mask(values)
    assert staged.run() == EXIT_REJECTED
    assert staged.json("error.json")["code"] == "input_format_invalid"
    assert staged.validate() == []


def test_too_large_lesion_fails_qc(staged):
    shape, _ = make_reference.working_grid(REGISTRY)
    staged.mask(np.ones(shape, np.uint8))  # 1.5 litres+
    (staged.in_dir / "params.json").write_text(
        json.dumps({"max_volume_ml": 100}), encoding="utf-8"
    )
    assert staged.run() == EXIT_REJECTED
    err = staged.json("error.json")
    assert (err["category"], err["code"]) == ("qc_failed", "lesion_too_large")
    assert err["details"]["max_volume_ml"] == 100
    assert staged.validate() == []


def test_small_lesion_gives_a_warning_not_a_failure(staged):
    shape, _ = make_reference.working_grid(REGISTRY)
    values = np.zeros(shape, np.uint8)
    values[5, 5, 5] = 1  # 8 mm3
    staged.mask(values)
    assert staged.run() == EXIT_OK
    warnings = staged.json("provenance.json")["warnings"]
    assert [w["code"] for w in warnings] == ["small_lesion"]
    assert staged.validate() == []


def test_unknown_parameter_is_rejected(staged):
    shape, _ = make_reference.working_grid(REGISTRY)
    staged.mask(make_reference.ball(shape))
    (staged.in_dir / "params.json").write_text('{"nope": 1}', encoding="utf-8")
    assert staged.run() == EXIT_REJECTED
    assert staged.json("error.json")["code"] == "invalid_parameters"


def test_missing_input_is_rejected(staged):
    assert staged.run() == EXIT_REJECTED
    assert staged.json("error.json")["code"] == "input_missing"


def test_output_is_deterministic(reference_dir, tmp_path):
    outputs = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        run = Staged(tmp_path / name)
        shutil.copyfile(
            reference_dir / "lesion_mask.nii.gz", run.in_dir / "lesion_mask.nii.gz"
        )
        assert run.run() == EXIT_OK
        outputs.append((run.out_dir / "lesion_qc.json").read_bytes())
    assert outputs[0] == outputs[1]


def test_internal_failure_code_is_not_an_exit_10(staged, monkeypatch):
    def boom(ctx):
        raise RuntimeError("boom")

    shape, _ = make_reference.working_grid(REGISTRY)
    staged.mask(make_reference.ball(shape))
    code = run_step(
        boom, staged.in_dir, staged.out_dir, manifest_path=MANIFEST, environ=staged.env
    )
    assert code == EXIT_INTERNAL
