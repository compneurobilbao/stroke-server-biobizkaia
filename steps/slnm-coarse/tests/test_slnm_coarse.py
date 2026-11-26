"""Tests of the slnm-coarse step wrapper on the synthetic reference dataset.

Tests marked ``upstream`` run the author's code unmodified (``slnm_fast`` of fast-slnm at the
commit of upstream.env, fetched with ``steps/fetch-upstream.sh steps/slnm-coarse DIR``) and
need ``SLNM_FAST_SRC=DIR``. Without it they are skipped, unless ``SLNM_FAST_REQUIRE_UPSTREAM``
is set (the container CI job does once the fetch succeeded), in which case a missing upstream
fails the run. On the host, ``antsApplyTransforms`` is replaced by a fake that resamples by
nearest neighbour with the identity transforms of the synthetic cache (``sitecustomize`` on
the subprocess's path); the container reference test runs the real ANTs. The other tests need
neither: they stop before the author's code runs or replace it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
import yaml

STEP_DIR = Path(__file__).resolve().parents[1]
REPO = STEP_DIR.parents[1]
sys.path.insert(0, str(STEP_DIR / "src"))

# loaded under a unique name: other steps also have a tests/make_reference.py
_spec = importlib.util.spec_from_file_location(
    "slnm_coarse_make_reference", STEP_DIR / "tests" / "make_reference.py"
)
make_reference = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = make_reference
_spec.loader.exec_module(make_reference)

from slnm_coarse_step import run as step_run  # noqa: E402
from slnm_coarse_step import step as step_module  # noqa: E402

from stroke_step_sdk import EXIT_INTERNAL, EXIT_OK, EXIT_REJECTED, contracts, run_step  # noqa: E402

MANIFEST = STEP_DIR / "step.yaml"
REGISTRY = contracts.contracts_dir() / "artefact-types.yaml"
SHAPE, AFFINE = make_reference.working_grid(REGISTRY)
PARAMS = {"workers": 2}


def _upstream_src() -> Path | None:
    env = os.environ.get("SLNM_FAST_SRC")
    if not env:
        return None
    src = Path(env) / "src"
    return src if (src / "slnm_fast" / "coarse" / "cohort_coarse_lesion.py").is_file() else None


UPSTREAM = _upstream_src()
if os.environ.get("SLNM_FAST_REQUIRE_UPSTREAM"):
    assert UPSTREAM is not None, "SLNM_FAST_REQUIRE_UPSTREAM is set but SLNM_FAST_SRC is not usable"
if UPSTREAM is not None and str(UPSTREAM) not in sys.path:
    sys.path.insert(0, str(UPSTREAM))
upstream = pytest.mark.skipif(UPSTREAM is None, reason="needs the author's code (SLNM_FAST_SRC)")

# Replaces antsApplyTransforms in the subprocesses of the author's code (host tests only): the
# synthetic transforms are identities, so the warp is a nearest-neighbour resampling of the
# input onto the reference grid.
FAKE_ANTS = textwrap.dedent(
    '''
    import subprocess

    _real_run = subprocess.run


    def _fake_ants(args):
        import nibabel as nib
        import numpy as np

        opts = dict(zip(args[1::2], args[2::2]))
        src, ref = nib.load(opts["-i"]), nib.load(opts["-r"])
        data = np.asanyarray(src.dataobj)
        ijk = np.indices(ref.shape[:3]).reshape(3, -1)
        world = ref.affine[:3, :3] @ ijk + ref.affine[:3, 3:4]
        inv = np.linalg.inv(src.affine)
        idx = np.rint(inv[:3, :3] @ world + inv[:3, 3:4]).astype(int)
        inside = np.all((idx >= 0) & (idx < np.array(data.shape)[:, None]), axis=0)
        out = np.zeros(ijk.shape[1], dtype=data.dtype)
        out[inside] = data[tuple(idx[:, inside])]
        nib.save(nib.Nifti1Image(out.reshape(ref.shape[:3]), ref.affine), opts["-o"])
        return subprocess.CompletedProcess(args, 0, "", "")


    def run(args, *a, **k):
        if isinstance(args, (list, tuple)) and args and args[0] == "antsApplyTransforms":
            return _fake_ants(list(map(str, args)))
        return _real_run(args, *a, **k)


    subprocess.run = run
    '''
)


@pytest.fixture(scope="session")
def reference(tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("slnm-coarse-reference")
    make_reference.generate(dest, REGISTRY)
    return dest


@pytest.fixture
def fake_ants(tmp_path_factory, monkeypatch):
    """PYTHONPATH of the subprocess: the fake ANTs and the author's code."""
    site = tmp_path_factory.mktemp("fake-ants")
    (site / "sitecustomize.py").write_text(FAKE_ANTS, encoding="utf-8")
    parts = [str(site), str(UPSTREAM)]
    if os.environ.get("PYTHONPATH"):
        parts.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(parts))


def execute(
    tmp_path: Path, mask: Path | None, bundle: Path, params: dict, cpus: int = 2
) -> tuple[int, Path]:
    in_dir, out_dir, ref = tmp_path / "in", tmp_path / "out", tmp_path / "ref"
    for d in (in_dir, out_dir, tmp_path / "tmp", ref):
        d.mkdir()
    shutil.copytree(bundle, ref / "cohort_cache")
    if mask is not None:
        shutil.copyfile(mask, in_dir / "lesion_mask.nii.gz")
    (in_dir / "params.json").write_text(json.dumps(params), encoding="utf-8")
    code = run_step(
        step_run,
        in_dir,
        out_dir,
        manifest_path=MANIFEST,
        ref_dir=ref,
        environ={"STEP_CPUS": str(cpus), "TMPDIR": str(tmp_path / "tmp")},
    )
    return code, out_dir


def error(out: Path) -> dict:
    return json.loads((out / "error.json").read_text(encoding="utf-8"))


def warnings(out: Path) -> dict[str, str]:
    prov = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    return {w["code"]: w["message"] for w in prov.get("warnings", [])}


def volume(path: Path) -> np.ndarray:
    return np.asanyarray(nib.load(str(path)).dataobj)


def assert_contract_valid(out: Path, *extra: str) -> None:
    issues = contracts.validator().validate_files(
        [MANIFEST, out / "provenance.json", *(out / e for e in extra)]
    )
    assert [i.message for i in issues if i.severity == "error"] == []


def copy_bundle(reference: Path, tmp_path: Path) -> Path:
    dest = tmp_path / "bundle-copy"
    shutil.copytree(reference / "bundle", dest)
    return dest


def save_mask(path: Path, mask: np.ndarray) -> Path:
    make_reference.write_nifti(path, mask.astype(np.uint8), AFFINE)
    return path


# --- manifest and reference data (no upstream needed) ------------------------------------------


def test_manifest_is_valid_contract_1_2():
    issues = contracts.validator().validate_files([MANIFEST])
    assert [i.message for i in issues if i.severity == "error"] == []
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert doc["contract_version"] == "1.2" and doc["version"] == "1.0.0"
    assert [o["type"] for o in doc["outputs"]] == ["sdisconnection_map", "sdisconnection_valid_mask"]
    assert doc["bundles"][0]["type"] == "slnm-coarse-cache"
    assert doc["parameters"]["properties"]["workers"]["default"] <= doc["resources"]["cpus"]
    third = {t["name"]: t for t in doc["licence"]["third_party"]}
    env = dict(
        line.split("=", 1)
        for line in (STEP_DIR / "upstream.env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert third["fast-slnm (slnm-coarse)"] == {
        "name": "fast-slnm (slnm-coarse)",
        "version": env["UPSTREAM_COMMIT"],
        "spdx": "MIT",
    }


def test_reference_bundle_is_a_valid_synthetic_bundle(reference):
    issues = contracts.validator().validate_files([reference / "bundle" / "bundle.yaml"])
    assert [i.message for i in issues if i.severity == "error"] == []
    doc = yaml.safe_load((reference / "bundle" / "bundle.yaml").read_text(encoding="utf-8"))
    assert doc["synthetic"] is True and doc["type"] == "slnm-coarse-cache"
    for entry in doc["files"]:
        data = (reference / "bundle" / entry["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]
    assert not any(p.suffix == ".tck" for p in (reference / "bundle").rglob("*"))


def test_reference_generation_is_deterministic(reference, tmp_path):
    make_reference.generate(tmp_path, REGISTRY)
    for rel in ("bundle/bundle.yaml", "lesion_mask.nii.gz", "expected/sdisconnection_map.nii.gz"):
        assert (tmp_path / rel).read_bytes() == (reference / rel).read_bytes()


def test_expected_result_has_valid_and_unsupported_parcels(reference):
    result = volume(reference / "expected" / "sdisconnection_map.nii.gz")
    valid = volume(reference / "expected" / "sdisconnection_valid_mask.nii.gz")
    atlas = make_reference.block_atlas(SHAPE)
    assert set(np.unique(atlas[valid == 0])) == {0, 2900, 2966}
    assert np.isnan(result[atlas == 2900]).all() and np.isnan(result[atlas == 2966]).all()
    assert (result[atlas == 0] == 0).all()
    inside = result[valid == 1]
    assert np.isfinite(inside).all() and (inside > 0).all() and (inside <= 1).all()


# --- wrapper behaviour without the author's code ------------------------------------------------


def test_empty_mask_is_rejected(reference, tmp_path):
    mask = save_mask(tmp_path / "empty.nii.gz", np.zeros(SHAPE))
    code, out = execute(tmp_path, mask, reference / "bundle", PARAMS)
    assert code == EXIT_REJECTED and error(out)["code"] == "mask_empty"
    assert_contract_valid(out, "error.json")


def test_lesion_outside_the_atlas_is_rejected(reference, tmp_path):
    mask = np.zeros(SHAPE)
    mask[5:8, 5:8, 5:8] = 1  # far from the block parcels
    code, out = execute(tmp_path, save_mask(tmp_path / "m.nii.gz", mask), reference / "bundle", PARAMS)
    assert code == EXIT_REJECTED and error(out)["code"] == "lesion_outside_atlas"
    assert_contract_valid(out, "error.json")


def test_missing_mask_and_bad_parameters_are_rejected(reference, tmp_path):
    (tmp_path / "a").mkdir()
    code, out = execute(tmp_path / "a", None, reference / "bundle", PARAMS)
    assert code == EXIT_REJECTED and error(out)["code"] == "input_missing"
    lesion = reference / "lesion_mask.nii.gz"
    (tmp_path / "b").mkdir()
    code, out = execute(tmp_path / "b", lesion, reference / "bundle", {"workers": 0})
    assert code == EXIT_REJECTED and error(out)["code"] == "invalid_parameters"


def test_atlas_off_the_working_grid_is_a_bundle_failure(reference, tmp_path):
    bundle = copy_bundle(reference, tmp_path)
    atlas = bundle / "cache" / make_reference.ATLAS_FILENAME
    shifted = AFFINE.copy()
    shifted[0, 3] += 2
    make_reference.write_nifti(atlas, make_reference.block_atlas(SHAPE), shifted)
    code, out = execute(tmp_path, reference / "lesion_mask.nii.gz", bundle, PARAMS)
    assert code == EXIT_INTERNAL and error(out)["code"] == "bundle_layout_unsupported"


def test_atlas_without_the_last_label_is_a_bundle_failure(reference, tmp_path):
    bundle = copy_bundle(reference, tmp_path)
    labels = make_reference.block_atlas(SHAPE)
    labels[labels == 2966] = 2965
    make_reference.write_nifti(bundle / "cache" / make_reference.ATLAS_FILENAME, labels, AFFINE)
    code, out = execute(tmp_path, reference / "lesion_mask.nii.gz", bundle, PARAMS)
    assert code == EXIT_INTERNAL and error(out)["code"] == "bundle_layout_unsupported"


def test_cohort_without_a_site_is_refused_before_running(reference, tmp_path):
    bundle = copy_bundle(reference, tmp_path)
    rows = (bundle / "participants.csv").read_text(encoding="utf-8").splitlines()
    kept = [r for r in rows if not r.endswith(",WashU")]
    (bundle / "participants.csv").write_text("\n".join(kept) + "\n", encoding="utf-8")
    code, out = execute(tmp_path, reference / "lesion_mask.nii.gz", bundle, PARAMS)
    assert code == EXIT_INTERNAL and error(out)["code"] == "cohort_insufficient"


def test_participant_without_cache_is_a_bundle_failure(reference, tmp_path):
    bundle = copy_bundle(reference, tmp_path)
    shutil.rmtree(next((bundle / "cache").glob("*_DiffusionRecommended")))
    code, out = execute(tmp_path, reference / "lesion_mask.nii.gz", bundle, PARAMS)
    assert code == EXIT_INTERNAL and error(out)["code"] == "bundle_layout_unsupported"
    assert error(out)["details"]["missing_participant_caches"] == 1


def test_failure_of_the_authors_code_is_reported_without_paths_or_identifiers(
    reference, tmp_path, monkeypatch
):
    monkeypatch.setattr(step_module, "UPSTREAM_MODULE", "slnm_coarse_step.no_such_module")
    code, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    assert code == EXIT_INTERNAL and error(out)["code"] == "slnm_coarse_failed"
    assert str(tmp_path) not in json.dumps(error(out))
    assert step_module.scrub("bad cache: /ref/x/HCA9_V1_MR_DiffusionRecommended") == (
        "bad cache: <path>"
    )
    assert step_module.scrub("Invalid participant HCA123_V1_MR (MGH)") == (
        "Invalid participant <participant> (MGH)"
    )


def test_workers_are_capped_by_the_granted_cpus(reference, tmp_path, monkeypatch):
    seen = {}

    def fake(cache, participants, lesion, output, workers, log):
        seen["workers"] = workers
        raise step_module.InternalFailure("slnm_coarse_failed", "stop here")

    monkeypatch.setattr(step_module, "run_slnm_coarse", fake)
    execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", {"workers": 8}, cpus=3)
    assert seen["workers"] == 3


def test_group_outputs_mark_unsupported_parcels_as_nan_not_zero(tmp_path):
    atlas = np.zeros((4, 4, 4), dtype=np.int32)
    atlas[0] = 5
    atlas[1] = 7
    final = np.zeros(atlas.shape, dtype=np.float32)
    final[0] = 0.25
    valid = (atlas == 5).astype(np.float32)
    nib.save(nib.Nifti1Image(final, np.eye(4)), tmp_path / "lesion_slnm.nii.gz")
    nib.save(nib.Nifti1Image(valid, np.eye(4)), tmp_path / "valid_mask.nii.gz")
    result, mask, counts = step_module.group_outputs(tmp_path, atlas)
    assert (result[0] == 0.25).all() and np.isnan(result[1]).all() and (result[2:] == 0).all()
    assert mask.dtype == np.uint8 and (mask[0] == 1).all() and mask[1:].sum() == 0
    assert counts == {"atlas_parcels": 2, "valid_parcels": 1}


# --- the author's code ------------------------------------------------------------------------------


@upstream
def test_constants_are_those_of_the_authors_code():
    from slnm_fast import cohort_inputs

    assert step_module.ATLAS_FILENAME == cohort_inputs.ATLAS_FILENAME
    assert step_module.N_LABELS == cohort_inputs.N_LABELS
    assert step_module.SITES == cohort_inputs.SITES
    assert make_reference.ATLAS_FILENAME == cohort_inputs.ATLAS_FILENAME
    assert make_reference.SITES == cohort_inputs.SITES


@upstream
def test_independent_expected_values_agree_with_the_authors_functions(reference):
    """The expected map of make_reference (numpy, by definition) equals what the author's
    functions give on the same synthetic cache (direct calls, no ANTs: identity transforms)."""
    from slnm_fast.coarse.approximate_roi_index_map import (
        approximate_passing_fraction,
        exposure_probabilities,
    )
    from slnm_fast.coarse.cohort_coarse_lesion import balanced_final_values, stats_by_site

    cache = reference / "bundle" / "cache"
    lesion_native = make_reference.crop(make_reference.lesion_mask(SHAPE)) > 0
    scores, support, sites = [], [], []
    for label, site in make_reference.PARTICIPANTS:
        root = cache / f"{label}_DiffusionRecommended"
        native = volume(root / "atlas_dwispace.nii.gz").astype(np.int32)
        offsets = np.load(root / "index" / "roi_offsets.npy")
        ids = np.load(root / "index" / "track_ids.npy")
        totals = np.load(root / "index" / "roi_weight_totals.npy")
        weights = np.fromfile(root / "streamlines" / "sift2_weights.txt", sep=" ")
        exposure = exposure_probabilities(native, lesion_native)
        scores.append(approximate_passing_fraction(exposure, offsets, ids, weights, totals))
        s = totals > 0
        s[0] = False
        support.append(s)
        sites.append(site)
    stats = stats_by_site(np.array(scores), np.array(support), sites)
    final = balanced_final_values(stats)
    atlas = make_reference.block_atlas(SHAPE)
    expected = volume(reference / "expected" / "sdisconnection_map.nii.gz")
    for label in make_reference.LABELS:
        value = expected[atlas == label][0]
        assert bool(stats["valid_mask"][label]) == bool(np.isfinite(value))
        if np.isfinite(value):
            assert final[label] == pytest.approx(value, rel=1e-5, abs=1e-6)


@upstream
def test_reference_case_with_the_authors_code(reference, tmp_path, fake_ants):
    code, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    assert code == EXIT_OK, (out / "provenance.json").read_text(encoding="utf-8")
    assert_contract_valid(out)
    result = volume(out / "sdisconnection_map.nii.gz")
    expected = volume(reference / "expected" / "sdisconnection_map.nii.gz")
    np.testing.assert_allclose(result, expected, rtol=1e-5, atol=1e-6, equal_nan=True)
    np.testing.assert_array_equal(
        volume(out / "sdisconnection_valid_mask.nii.gz"),
        volume(reference / "expected" / "sdisconnection_valid_mask.nii.gz"),
    )
    found = warnings(out)
    assert "2 of 8 atlas parcels" in found["unsupported_parcels"]
    summary = json.loads((out / "extra" / "slnm_coarse_summary.json").read_text(encoding="utf-8"))
    assert summary["valid_parcels"] == 6 and summary["participants"] == 9
    assert "lesion" not in summary
    # nothing published names a participant of the normative cohort or a container path
    for path in out.rglob("*"):
        if path.is_file() and path.suffix in (".json", ".csv"):
            text = path.read_text(encoding="utf-8")
            assert "HCASYNTH" not in text and str(tmp_path) not in text
