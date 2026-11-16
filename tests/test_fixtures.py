"""Tests for the synthetic fixture generator (card F0-20)."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
import yaml

from contracts import validate as v
from fixtures import generate as gen
from fixtures.io import MNI2_AFFINE, MNI2_SHAPE, payload_sha256, sha256_file

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "fixtures" / "data"
BUNDLES = sorted(DATA.glob("bundles/*/*/bundle.yaml"))
MNI_MASKS = sorted((DATA / "mni_masks").glob("*.nii.gz"))
CASES = sorted(p for p in (DATA / "cases").iterdir() if p.is_dir())


@pytest.fixture(scope="module")
def regenerated(tmp_path_factory):
    a = tmp_path_factory.mktemp("gen_a")
    b = tmp_path_factory.mktemp("gen_b")
    gen.generate(a)
    gen.generate(b)
    return a, b


def _files(root: Path) -> list[str]:
    return sorted(
        p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()
    )


def test_two_runs_are_byte_identical(regenerated):
    a, b = regenerated
    assert _files(a) == _files(b)
    for rel in _files(a):
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), rel


def _manifest_without_hashes(path: Path) -> dict:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc.pop("content_digest")
    doc["files"] = [f["path"] for f in doc["files"]]
    return doc


def test_regeneration_matches_committed_checksums(regenerated):
    a, _ = regenerated
    committed = json.loads((DATA / "CHECKSUMS.json").read_text(encoding="utf-8"))
    fresh = json.loads((a / "CHECKSUMS.json").read_text(encoding="utf-8"))
    assert fresh["files"].keys() == committed["files"].keys()
    for rel, entry in committed["files"].items():
        if rel.endswith("bundle.yaml"):
            # Lists hashes of stored (gzip) bytes, which depend on the zlib build:
            # compare everything except the hashes (the payloads are checked above).
            assert _manifest_without_hashes(a / rel) == _manifest_without_hashes(
                DATA / rel
            )
        else:
            assert fresh["files"][rel]["payload_sha256"] == entry["payload_sha256"], rel


def test_committed_files_match_checksum_manifest():
    committed = json.loads((DATA / "CHECKSUMS.json").read_text(encoding="utf-8"))
    assert _files(DATA) == sorted([*committed["files"], "CHECKSUMS.json"])
    for rel, entry in committed["files"].items():
        p = DATA / rel
        assert sha256_file(p) == entry["sha256"], rel
        assert payload_sha256(p) == entry["payload_sha256"], rel
        assert p.stat().st_size == entry["size_bytes"], rel


def test_committed_size_budget():
    total = sum(p.stat().st_size for p in DATA.rglob("*") if p.is_file())
    assert total < 5 * 1024 * 1024


def test_grid_constants_match_registry():
    reg = yaml.safe_load((ROOT / "contracts" / "artefact-types.yaml").read_text())
    grid = reg["grids"]["MNI152NLin6Asym-res2"]
    assert tuple(grid["shape"]) == MNI2_SHAPE
    assert np.allclose(np.array(grid["affine"]), MNI2_AFFINE, atol=1e-4)


@pytest.mark.parametrize("bundle", BUNDLES, ids=lambda p: p.parts[-3])
def test_bundle_validates_and_matches_disk(bundle):
    assert v.validate_files([bundle], None, v.DEFAULT_TYPES_PATH, False) == []
    doc = yaml.safe_load(bundle.read_text(encoding="utf-8"))
    assert doc["synthetic"] is True
    assert doc["licence"]["redistributable"] is True
    listed = {f["path"] for f in doc["files"]}
    on_disk = {
        p.relative_to(bundle.parent).as_posix()
        for p in bundle.parent.rglob("*")
        if p.is_file() and p.name != "bundle.yaml"
    }
    assert listed == on_disk
    for f in doc["files"]:
        p = bundle.parent / f["path"]
        assert p.stat().st_size == f["size_bytes"]
        assert sha256_file(p) == f["sha256"]
    assert doc["content_digest"] == v.bundle_digest(doc["files"])


def _check_grid_mask(path: Path, *, mni: bool) -> np.ndarray:
    img = nib.load(path)
    data = np.asarray(img.dataobj)
    assert set(np.unique(data)) <= {0, 1}, path
    if mni:
        assert img.shape == MNI2_SHAPE
        assert np.allclose(img.affine, MNI2_AFFINE, atol=1e-4)
    return data


@pytest.mark.parametrize("path", MNI_MASKS, ids=lambda p: p.name)
def test_mni_masks_are_working_grid_lesion_masks(path):
    data = _check_grid_mask(path, mni=True)
    assert data.sum() > 0


def test_mni_masks_vary_in_volume_and_side():
    rows = (DATA / "mni_masks" / "index.tsv").read_text().splitlines()[1:]
    sides = {r.split("\t")[1] for r in rows}
    vols = [float(r.split("\t")[2]) for r in rows]
    assert sides == {"left", "right"}
    assert min(vols) < 3 and max(vols) > 50


@pytest.mark.parametrize("case", CASES, ids=lambda p: p.name)
def test_case_artefacts(case):
    ct = nib.load(case / "ct.nii.gz")
    mask = _check_grid_mask(case / "lesion_mask_native.nii.gz", mni=False)
    nat = nib.load(case / "lesion_mask_native.nii.gz")
    assert nat.shape == ct.shape and np.allclose(nat.affine, ct.affine)
    ct_data = np.asarray(ct.dataobj)
    assert ct_data.dtype == np.int16 and ct_data.min() < -900 and ct_data.max() > 500
    assert mask.sum() > 0
    # The lesion lies in white matter (28 HU) lowered by the lesion offset.
    assert ct_data[mask > 0].mean() < 25
    truth = _check_grid_mask(case / "lesion_mask_mni.nii.gz", mni=True)
    info = json.loads((case / "truth.json").read_text())
    assert info["lesion"]["volume_ml_mni"] == gen.volume_ml(truth)
    x = np.argwhere(truth)[:, 0].mean()  # voxel i; x = -2 i + 90
    assert (-2 * x + 90 < 0) == (info["lesion"]["side"] == "left")


def test_nifti_gzip_has_no_timestamp():
    raw = (DATA / "cases" / "case-01" / "ct.nii.gz").read_bytes()
    assert raw[:2] == b"\x1f\x8b" and raw[4:8] == b"\x00\x00\x00\x00"
    assert gzip.decompress(raw)[344:348] in (b"n+1\x00", b"ni1\x00")


def test_connectome_bundle_content():
    root = DATA / "bundles" / "toy-normative-connectome" / "1.0.0"
    meta = yaml.safe_load((root / "bundle.yaml").read_text())["metadata"]
    z = np.load(root / "connectomes.npz")
    mats = z["connectivity"]
    n, p = meta["n_participants"], meta["n_parcels"]
    assert mats.shape == (n, p, p) and mats.dtype == np.float32
    assert np.allclose(mats, mats.transpose(0, 2, 1)) and np.all(
        mats.diagonal(0, 1, 2) == 0
    )
    rows = [
        r.split("\t") for r in (root / "participants.tsv").read_text().splitlines()[1:]
    ]
    assert [r[0] for r in rows] == list(z["participant_id"])
    assert sum(r[1] == "F" for r in rows) == meta["n_female"]
    ages = [int(r[2]) for r in rows]
    assert meta["age_range"] == f"{min(ages)}-{max(ages)}"
    atlas = np.asarray(nib.load(root / "parcellation" / "atlas.nii.gz").dataobj)
    assert atlas.shape == MNI2_SHAPE and set(np.unique(atlas)) == set(range(p + 1))


def test_tract_bundle_reference_disconnection():
    root = DATA / "bundles" / "toy-tract-atlas" / "1.0.0"
    # Layout read by the lnm-tracts step: labels.txt and one <tract>.tck per label.
    names = (root / "labels.txt").read_text().split()
    assert names == sorted(set(names)) and len(names) == 4
    on_disk = {p.name for p in root.iterdir()}
    assert on_disk == {
        "bundle.yaml",
        "README.md",
        "labels.txt",
        *(f"{n}.tck" for n in names),
    }
    for name in names:
        tck = nib.streamlines.load(str(root / f"{name}.tck"), lazy_load=False)
        assert len(tck.streamlines) == 60, name
        assert all(s.dtype == np.float32 and s.shape[1] == 3 for s in tck.streamlines)
    lesion = np.asarray(
        nib.load(DATA / "mni_masks" / "lesion_right_large.nii.gz").dataobj
    )
    d = gen.tract_disconnection(root, lesion)
    assert d["toy_cst_left"] == 0.0
    assert d["toy_cst_right"] > 0.0
    assert all(0.0 <= x <= 1.0 for x in d.values())
