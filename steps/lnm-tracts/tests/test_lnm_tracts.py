"""Tests of the tract-wise disconnection step.

The container paths (Docker, Apptainer, the legacy script of the lnm repository, the
equivalence with the previously published image) are exercised by the lnm-tracts job of
.github/workflows/steps.yml. Here the step runs in-process through the SDK. Tests that need
the MRtrix3 commands are skipped when ``tckedit`` is not on PATH.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

STEP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STEP_DIR / "src"))

# loaded under a unique name: other steps also have a tests/make_reference.py
_spec = importlib.util.spec_from_file_location(
    "lnm_tracts_make_reference", STEP_DIR / "tests" / "make_reference.py"
)
make_reference = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = make_reference
_spec.loader.exec_module(make_reference)

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


# --- atlas space (step 1.1.0) ------------------------------------------------------------------

from lnm_tracts_step import space as space_module  # noqa: E402


def use_space_bundles(staged: Staged, reference: Path, transform: str | None) -> None:
    """Mount the toy atlas that declares ToyAtlasSpace and, if given, a space-transform bundle."""
    shutil.rmtree(staged.bundle)
    shutil.copytree(reference / "space" / "atlas", staged.bundle)
    if transform is not None:
        shutil.copytree(reference / "space" / transform, staged.ref_dir / "space_transform")


def test_toy_space_bundles_are_valid(reference):
    docs = [reference / "space" / d / "bundle.yaml" for d in ("atlas", "identity", "shift", "mismatch")]
    issues = contracts.validator().validate_files(docs)
    assert [i.message for i in issues if i.severity == "error"] == []


def test_atlas_space_is_declared_or_the_lesion_space(reference):
    read = space_module.read_bundle_doc
    assert space_module.atlas_space(read(reference / "bundle")) == "MNI152NLin6Asym"
    assert space_module.atlas_space(read(reference / "space" / "atlas")) == "ToyAtlasSpace"
    from stroke_step_sdk import InternalFailure

    with pytest.raises(InternalFailure):
        space_module.atlas_space({"metadata": {"space": ""}})


def test_nearest_resampling_identity_shift_and_upsampling():
    shape, affine = make_reference.working_grid(CONTRACTS)
    lesion = make_reference.ball(shape, affine, (-20, -15, 30), 10.0)
    ii, jj, kk = np.indices(shape)
    centres = np.stack([ii, jj, kk], -1) @ affine[:3, :3].T + affine[:3, 3]

    same = space_module.resample_nearest(lesion, affine, centres.astype(np.float32))
    assert np.array_equal(same, lesion)

    moved = space_module.resample_nearest(lesion, affine, (centres + [4, 0, 0]).astype(np.float32))
    assert np.array_equal(moved, make_reference.ball(shape, affine, (-24, -15, 30), 10.0))

    # 2 mm -> 1 mm on the same field of view: every 2 mm voxel becomes 8 voxels of 1 mm
    fine = np.diag([1.0, 1.0, 1.0, 1.0])
    fine[:3, 3] = affine[:3, 3] - 0.5 * np.sign(np.diag(affine)[:3])
    fine[:3, :3] = np.diag(np.sign(np.diag(affine)[:3]))
    fshape = tuple(2 * n for n in shape)
    fi, fj, fk = np.indices(fshape)
    fcentres = np.stack([fi, fj, fk], -1) @ fine[:3, :3].T + fine[:3, 3]
    up = space_module.resample_nearest(lesion, affine, fcentres.astype(np.float32))
    assert up.sum() == 8 * lesion.sum()

    # positions off the lesion grid or not finite are outside the lesion
    far = np.full(centres.shape, np.nan, np.float32)
    assert space_module.resample_nearest(lesion, affine, far).sum() == 0


def test_atlas_in_another_space_needs_the_transform(staged, reference):
    use_space_bundles(staged, reference, None)
    staged.lesion(reference / "cases" / "main.nii.gz")
    assert staged.run() == EXIT_INTERNAL
    err = staged.json("error.json")
    assert err["code"] == "space_transform_missing"
    assert not (staged.out_dir / "tract_disconnection.tsv").exists()
    assert staged.validate() == []


def test_transform_to_another_space_is_refused(staged, reference):
    use_space_bundles(staged, reference, "mismatch")
    staged.lesion(reference / "cases" / "main.nii.gz")
    assert staged.run() == EXIT_INTERNAL
    assert staged.json("error.json")["code"] == "space_transform_mismatch"
    assert staged.validate() == []


def test_lesion_outside_the_atlas_grid_is_rejected(staged, reference, tmp_path):
    use_space_bundles(staged, reference, None)
    shape, affine = make_reference.working_grid(CONTRACTS)
    make_reference.write_space_transform(
        staged.ref_dir / "space_transform", "toy-xfm-far", "ToyAtlasSpace", shape, affine, (1000, 0, 0)
    )
    staged.lesion(reference / "cases" / "main.nii.gz")
    assert staged.run() == EXIT_REJECTED
    err = staged.json("error.json")
    assert (err["category"], err["code"]) == ("input_rejected", "mask_outside_atlas_space")
    assert staged.validate() == []


@needs_mrtrix
@pytest.mark.parametrize("transform", ["identity", "shift"])
@pytest.mark.parametrize("case", ["main", "small"])
def test_lesion_is_resampled_into_the_atlas_space(transform, case, staged, reference):
    use_space_bundles(staged, reference, transform)
    staged.lesion(reference / "cases" / f"{case}.nii.gz")
    assert staged.run() == EXIT_OK
    problem = reftest.compare_allclose(
        staged.out_dir / "tract_disconnection.tsv",
        reference / "space" / "expected" / f"{transform}_{case}.tsv",
        rtol=0,
        atol=1e-6,
    )
    assert problem is None, problem
    assert staged.validate() == []
    record = staged.json("provenance.json")
    assert [b["name"] for b in record["bundles"]] == ["tract_atlas", "space_transform"]
    (warning,) = [w for w in record["warnings"] if w["code"] == "lesion_resampled_to_atlas_space"]
    detail = json.loads(
        (staged.out_dir / "extra" / "atlas_space_resampling.json").read_text(encoding="utf-8")
    )
    # the balls stay inside the grid: same voxel count before and after
    assert detail["voxels_before"] == detail["voxels_after"] > 0
    assert detail["volume_before_mm3"] == detail["volume_after_mm3"]
    assert f"{detail['voxels_before']} voxels" in warning["message"]
    assert (staged.out_dir / "extra" / "lesion_mask_atlas_space.nii.gz").is_file()


@needs_mrtrix
def test_transform_is_ignored_for_an_atlas_in_the_lesion_space(staged, reference):
    shutil.copytree(reference / "space" / "shift", staged.ref_dir / "space_transform")
    staged.lesion(reference / "cases" / "main.nii.gz")
    assert staged.run() == EXIT_OK
    problem = reftest.compare_allclose(
        staged.out_dir / "tract_disconnection.tsv",
        reference / "cases" / "expected" / "main.tsv",
        rtol=0,
        atol=1e-6,
    )
    assert problem is None, problem
    assert not (staged.out_dir / "extra").exists()
    assert "warnings" not in staged.json("provenance.json") or not staged.json("provenance.json")["warnings"]
