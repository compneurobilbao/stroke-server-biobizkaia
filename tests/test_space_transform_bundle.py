"""scripts/bundles/space_transform/make_bundle.py on synthetic inputs (card TRACTS-01).

The real build downloads the TemplateFlow templates and transform and runs ANTs
(build.sh); none of that is needed here. A synthetic target template, a source template moved
by a known shift and the ITK displacement field of that shift (LPS vectors, the layout
antsApplyTransforms writes) check the conversion to a RAS coordinate map, the verification
gates and that the written bundle.yaml is a valid bundle of type space-transform whose map
the lnm-tracts step reads.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
import yaml

from contracts import validate as v

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "bundles" / "space_transform" / "make_bundle.py"
SHIFT_RAS = np.array(
    [3.0, -2.0, 1.0]
)  # target voxel centre c maps to c + SHIFT_RAS in the source


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


mb = _load("space_transform_make_bundle", SCRIPT)


def _intensity(points: np.ndarray) -> np.ndarray:
    """A smooth, non-symmetric pattern in world millimetres."""
    x, y, z = points[..., 0], points[..., 1], points[..., 2]
    return (
        100.0 * np.exp(-((x - 4) ** 2 + (y + 6) ** 2 + (z - 2) ** 2) / 300.0)
        + 60.0 * np.exp(-((x + 10) ** 2 + (y - 8) ** 2 + (z + 5) ** 2) / 150.0)
        + 0.5 * x
    ).astype(np.float32)


@pytest.fixture
def work(tmp_path, monkeypatch) -> Path:
    work = tmp_path / "work"
    dl = work / "dl"
    shape = (30, 34, 28)
    affine = np.array(
        [[-1.0, 0, 0, 15.0], [0, 1.0, 0, -17.0], [0, 0, 1.0, -14.0], [0, 0, 0, 1]]
    )
    centres = mb.voxel_centres(shape, affine)
    target = _intensity(centres)
    # The source template is the target moved by SHIFT_RAS: source(c + s) == target(c).
    source = _intensity(centres - SHIFT_RAS)
    mask = np.zeros(shape, np.uint8)
    mask[6:-6, 6:-6, 6:-6] = 1
    files = {
        "target_t1w": nib.Nifti1Image(target, affine),
        "target_mask": nib.Nifti1Image(mask, affine),
        "source_t1w": nib.Nifti1Image(source, affine),
    }
    sources = {}
    for key, (rel, _digest) in mb.SOURCES.items():
        path = dl / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if key == "xfm":
            path.write_bytes(b"synthetic transform placeholder\n")
        elif key == "licence":
            path.write_bytes(b"Synthetic licence text for the test.\n")
        else:
            nib.save(files[key], str(path))
        sources[key] = (rel, mb.sha256(path))
    monkeypatch.setattr(mb, "SOURCES", sources)

    # What antsApplyTransforms writes: an ITK displacement field (LPS vectors, X, Y, Z, 1, 3)
    # on the target grid, and the source template resampled with the transform.
    ants = work / "ants"
    ants.mkdir(parents=True)
    lps = np.broadcast_to(SHIFT_RAS * mb.LPS_TO_RAS, (*shape, 1, 3)).astype(np.float32)
    nib.save(
        nib.Nifti1Image(np.ascontiguousarray(lps), affine),
        str(ants / "displacement.nii.gz"),
    )
    resampled = mb.trilinear(
        files["source_t1w"], (centres + SHIFT_RAS).astype(np.float32)
    )
    nib.save(
        nib.Nifti1Image(resampled, affine), str(ants / "nlin6asym_T1w_in_2009a.nii.gz")
    )
    return work


def test_coordinate_map_is_the_ras_position_in_the_source_space(work, tmp_path):
    out = tmp_path / "bundle"
    result = mb.write_bundle(work, out, "test")
    img = nib.load(str(out / "coordinate_map.nii.gz"))
    coords = np.asarray(img.dataobj)
    assert coords.shape == (30, 34, 28, 3)
    centres = mb.voxel_centres(coords.shape[:3], img.affine)
    np.testing.assert_allclose(coords, centres + SHIFT_RAS, atol=1e-4)
    checks = json.loads((out / "verification.json").read_text(encoding="utf-8"))
    assert checks == result["checks"]
    tc = checks["t1w_correlation_with_target_in_brain"]
    assert tc["with_transform"] > tc["without_transform"]
    assert checks["displacement_mm_in_brain"]["max"] == pytest.approx(
        float(np.linalg.norm(SHIFT_RAS)), abs=1e-3
    )


def test_bundle_yaml_is_a_valid_space_transform_bundle(work, tmp_path):
    out = tmp_path / "bundle"
    mb.write_bundle(work, out, "test")
    issues = v.validate_files([out / "bundle.yaml"])
    assert [i.message for i in issues if i.severity == "error"] == []
    doc = yaml.safe_load((out / "bundle.yaml").read_text(encoding="utf-8"))
    assert doc["type"] == "space-transform"
    assert doc["metadata"]["from_space"] == "MNI152NLin6Asym"
    assert doc["metadata"]["to_space"] == "MNI152NLin2009aAsym"
    assert doc["synthetic"] is False and doc["licence"]["redistributable"] is True
    # every file is listed with its checksum, and the content digest covers them
    listed = {f["path"]: f["sha256"] for f in doc["files"]}
    assert set(listed) == {
        "LICENSE",
        "README.md",
        "coordinate_map.nii.gz",
        "verification.json",
    }
    for rel, digest in listed.items():
        assert mb.sha256(out / rel) == digest
    lines = "".join(f"{f['sha256']}  {f['path']}\n" for f in doc["files"])
    assert (
        doc["content_digest"] == "sha256:" + hashlib.sha256(lines.encode()).hexdigest()
    )
    assert (out / "LICENSE").read_bytes() == b"Synthetic licence text for the test.\n"


def test_the_lnm_tracts_step_reads_the_bundle(work, tmp_path):
    pytest.importorskip(
        "stroke_step_sdk"
    )  # installed in the steps CI jobs, not in the backend one
    out = tmp_path / "bundle"
    mb.write_bundle(work, out, "test")
    space = _load(
        "lnm_tracts_space_for_bundle_test",
        ROOT / "steps" / "lnm-tracts" / "src" / "lnm_tracts_step" / "space.py",
    )
    transform = space.load_transform(out, "MNI152NLin6Asym", "MNI152NLin2009aAsym")
    assert transform.coords.shape == (30, 34, 28, 3)
    assert (transform.bundle_id, transform.version) == (mb.BUNDLE_ID, mb.BUNDLE_VERSION)


def test_a_wrong_conversion_is_refused(work, tmp_path):
    """The ANTs resampling check catches a coordinate map built with the wrong sign."""
    ants = work / "ants" / "displacement.nii.gz"
    img = nib.load(str(ants))
    nib.save(nib.Nifti1Image(-np.asarray(img.dataobj), img.affine), str(ants))
    with pytest.raises(SystemExit, match="does not reproduce the ANTs resampling"):
        mb.write_bundle(work, tmp_path / "bundle", "test")
