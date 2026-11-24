"""Tests of the tract-wise disconnection step.

The container paths (Docker, Apptainer, the legacy script) are exercised by
.github/workflows/step.yml. Here the step runs in-process through the SDK. Tests that need
the MRtrix3 commands are skipped when ``tckedit`` is not on PATH.
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
from lnm_tracts_step import run as step_run  # noqa: E402
from lnm_tracts_step import step as step_module  # noqa: E402

from stroke_step_sdk import (  # noqa: E402
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_REJECTED,
    contracts,
    reftest,
    run_step,
)

CONTRACTS = contracts.contracts_dir()
MANIFEST = STEP_DIR / "step.yaml"
needs_mrtrix = pytest.mark.skipif(
    shutil.which("tckedit") is None or shutil.which("tckstats") is None,
    reason="MRtrix3 is not installed (it is in the step image)",
)


@pytest.fixture(scope="module")
def reference(tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("reference")
    make_reference.generate(dest, CONTRACTS)
    return dest


class Staged:
    def __init__(self, root: Path, bundle: Path) -> None:
        self.in_dir, self.out_dir, self.ref_dir = root / "in", root / "out", root / "ref"
        for d in (self.in_dir, self.out_dir, root / "tmp", self.ref_dir):
            d.mkdir()
        (self.in_dir / "params.json").write_text("{}", encoding="utf-8")
        self.bundle = self.ref_dir / "tract_atlas"
        shutil.copytree(bundle, self.bundle)
        self.env = {"STEP_CPUS": "1", "TMPDIR": str(root / "tmp")}

    def lesion(self, source: Path) -> None:
        shutil.copyfile(source, self.in_dir / "lesion_mask.nii.gz")

    def run(self) -> int:
        return run_step(
            step_run,
            self.in_dir,
            self.out_dir,
            manifest_path=MANIFEST,
            ref_dir=self.ref_dir,
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
def staged(tmp_path, reference) -> Staged:
    return Staged(tmp_path, reference / "bundle")


def read_table(path: Path) -> dict[str, dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    head = lines[0].split("\t")
    return {r.split("\t")[0]: dict(zip(head, r.split("\t"))) for r in lines[1:]}


# --- manifest, generated data, pure functions ----------------------------------------------


def test_manifest_is_valid_and_declares_standard_codes():
    from stroke_step_sdk.manifest import load_manifest

    assert load_manifest(MANIFEST).missing_sdk_codes() == []


def test_toy_dataset_is_non_trivial(reference):
    """The oracle gives partial, full and zero disconnection, so the comparisons mean something."""
    values = {
        case: [
            float(r["disconnection"])
            for r in read_table(reference / "cases" / "expected" / f"{case}.tsv").values()
        ]
        for case in ("main", "small", "wide", "none")
    }
    assert any(0 < v < 1 for v in values["main"])
    assert 1.0 in values["wide"] and any(0 < v < 1 for v in values["wide"])
    assert set(values["none"]) == {0.0}
    assert any(v > 0 for v in values["small"])


def test_toy_bundle_is_a_valid_bundle(reference):
    issues = contracts.validator().validate_files([reference / "bundle" / "bundle.yaml"])
    assert [i.message for i in issues if i.severity == "error"] == []


def test_disconnection_is_the_removed_fraction():
    assert step_module.disconnection(60, 9) == pytest.approx(0.85)
    assert step_module.disconnection(45, 45) == 0.0
    assert step_module.disconnection(45, 0) == 1.0


def test_read_labels(reference, tmp_path):
    names = step_module.read_labels(reference / "bundle")
    assert names == [t[0] for t in make_reference.TRACTS]

    def broken(edit) -> Path:
        d = tmp_path / f"b{len(list(tmp_path.iterdir()))}"
        shutil.copytree(reference / "bundle", d)
        edit(d)
        return d

    from stroke_step_sdk import InternalFailure

    cases = {
        "no labels": lambda d: (d / "labels.txt").unlink(),
        "empty labels": lambda d: (d / "labels.txt").write_text("\n\n"),
        "duplicate": lambda d: (d / "labels.txt").write_text("toy_cc_body\ntoy_cc_body\n"),
        "path in name": lambda d: (d / "labels.txt").write_text("../toy_cc_body\n"),
        "missing tck": lambda d: (d / "toy_cc_body.tck").unlink(),
    }
    for label, edit in cases.items():
        with pytest.raises(InternalFailure) as exc:
            step_module.read_labels(broken(edit))
        assert exc.value.code == "bundle_invalid", label


# --- contract behaviour that needs no MRtrix -------------------------------------------------


def test_empty_mask_is_rejected(staged, reference):
    shape, affine = make_reference.working_grid(CONTRACTS)
    nib.save(
        nib.Nifti1Image(np.zeros(shape, np.uint8), affine),
        str(staged.in_dir / "lesion_mask.nii.gz"),
    )
    assert staged.run() == EXIT_REJECTED
    err = staged.json("error.json")
    assert (err["category"], err["code"], err["port"]) == (
        "input_rejected",
        "mask_empty",
        "lesion_mask",
    )
    assert staged.validate() == []


def test_non_binary_mask_is_rejected_by_the_sdk(staged):
    shape, affine = make_reference.working_grid(CONTRACTS)
    values = np.zeros(shape, np.float32)
    values[10:12, 10:12, 10:12] = 255
    nib.save(nib.Nifti1Image(values, affine), str(staged.in_dir / "lesion_mask.nii.gz"))
    assert staged.run() == EXIT_REJECTED
    assert staged.json("error.json")["code"] == "input_format_invalid"
    assert staged.validate() == []


def test_missing_mrtrix_is_an_internal_failure(staged, reference, monkeypatch):
    staged.lesion(reference / "cases" / "main.nii.gz")
    monkeypatch.setenv("PATH", str(staged.in_dir))  # a directory without tckstats
    assert staged.run() == EXIT_INTERNAL
    assert staged.json("error.json")["code"] == "mrtrix_unavailable"
    assert not (staged.out_dir / "tract_disconnection.tsv").exists()


# --- full runs with MRtrix3 --------------------------------------------------------------------


@needs_mrtrix
@pytest.mark.parametrize("case", ["main", "small", "wide", "none"])
def test_tables_match_the_numpy_oracle(case, staged, reference):
    staged.lesion(reference / "cases" / f"{case}.nii.gz")
    assert staged.run() == EXIT_OK
    out = staged.out_dir / "tract_disconnection.tsv"
    problem = reftest.compare_allclose(
        out, reference / "cases" / "expected" / f"{case}.tsv", rtol=0, atol=1e-6
    )
    assert problem is None, problem
    assert sorted(p.name for p in staged.out_dir.iterdir()) == [
        "provenance.json",
        "tract_disconnection.tsv",
    ]
    assert staged.validate() == []
    record = staged.json("provenance.json")
    assert record["status"] == "succeeded"
    assert [b["id"] for b in record["bundles"]] == ["toy-lnm-tract-atlas"]
    assert "mrtrix3" in {s["name"] for s in record["software"]}


@needs_mrtrix
def test_run_does_not_touch_the_atlas_or_leave_scratch(staged, reference):
    def fingerprint():
        return {p.name: p.read_bytes() for p in staged.bundle.iterdir()}

    before = fingerprint()
    staged.lesion(reference / "cases" / "main.nii.gz")
    assert staged.run() == EXIT_OK
    assert fingerprint() == before
    assert list((staged.out_dir.parent / "tmp").iterdir()) == []
