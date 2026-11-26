"""Tests of the slnm-coarse-cache bundle builder on the synthetic cache (no real data)."""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

STEP_DIR = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


make_reference = _load("slnm_coarse_make_reference_b", STEP_DIR / "tests" / "make_reference.py")
builder = _load("slnm_coarse_build_bundle", STEP_DIR / "bundle" / "build_bundle.py")

from stroke_step_sdk import contracts  # noqa: E402

REGISTRY = contracts.contracts_dir() / "artefact-types.yaml"


@pytest.fixture(scope="module")
def coarse_files(tmp_path_factory) -> Path:
    """The synthetic cache laid out as the author's coarse_files directory, with the extra
    files of a real one (a tractogram and an inverse warp) that must not be copied."""
    ref = tmp_path_factory.mktemp("ref")
    make_reference.generate(ref, REGISTRY)
    root = ref / "bundle" / "cache"
    for subject in root.glob("*_DiffusionRecommended"):
        (subject / "streamlines" / "streamlines_iFOD2.tck").write_bytes(b"not copied")
        (subject / "dwireg" / "standard2dwi1InverseWarp.nii.gz").write_bytes(b"not copied")
    table = ref / "participants.csv"
    lines = ["subject_label,site,age"] + [f"{label},{site},70" for label, site in make_reference.PARTICIPANTS]
    table.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def args(coarse_files: Path, dest: Path, participants: Path | None = None) -> list[str]:
    return [
        "--coarse-files", str(coarse_files),
        "--participants", str(participants or coarse_files.parents[1] / "participants.csv"),
        "--dest", str(dest),
        "--id", "test-slnm-coarse-cache",
        "--version", "1.0.0",
        "--licence-spdx", "LicenseRef-Test",
    ]  # fmt: skip


def test_builds_a_valid_bundle_with_only_the_files_slnm_coarse_reads(coarse_files, tmp_path):
    dest = tmp_path / "bundle"
    assert builder.main(args(coarse_files, dest)) == 0
    issues = contracts.validator().validate_files([dest / "bundle.yaml"])
    assert [i.message for i in issues if i.severity == "error"] == []
    doc = yaml.safe_load((dest / "bundle.yaml").read_text(encoding="utf-8"))
    assert doc["type"] == "slnm-coarse-cache" and doc["licence"]["redistributable"] is False
    assert doc["synthetic"] is False and doc["metadata"]["n_participants"] == 9
    paths = {f["path"] for f in doc["files"]}
    assert not any(p.endswith((".tck", "InverseWarp.nii.gz")) for p in paths)
    assert len(paths) == 2 + 9 * len(builder.PARTICIPANT_FILES)
    # the extra column of the source table is dropped
    assert (dest / "participants.csv").read_text(encoding="utf-8").splitlines()[0] == "subject_label,site"


def test_refuses_a_cache_that_is_not_portable(coarse_files, tmp_path):
    source = tmp_path / "coarse"
    shutil.copytree(coarse_files, source)
    meta_path = next(source.glob("*_DiffusionRecommended")) / "metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["inputs_mode"] = "symlink"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    participants = coarse_files.parents[1] / "participants.csv"
    assert builder.main(args(source, tmp_path / "bundle", participants)) == 1


def test_refuses_a_table_without_every_site(coarse_files, tmp_path):
    table = tmp_path / "participants.csv"
    rows = [f"{label},{site}" for label, site in make_reference.PARTICIPANTS if site != "UCLA"]
    table.write_text("subject_label,site\n" + "\n".join(rows) + "\n", encoding="utf-8")
    assert builder.main(args(coarse_files, tmp_path / "bundle", table)) == 1


def test_refuses_an_atlas_off_the_working_grid(coarse_files, tmp_path):
    source = tmp_path / "coarse"
    shutil.copytree(coarse_files, source)
    shape, affine = make_reference.working_grid(REGISTRY)
    affine = affine.copy()
    affine[2, 3] += 2
    make_reference.write_nifti(source / builder.ATLAS_FILENAME, make_reference.block_atlas(shape), affine)
    participants = coarse_files.parents[1] / "participants.csv"
    assert builder.main(args(source, tmp_path / "bundle", participants)) == 1
