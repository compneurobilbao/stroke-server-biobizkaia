"""Tests of the COGNET-STROKE wrapper on the synthetic reference dataset (generated, not committed).

Everything runs in-process with the author's ``src/cdr.py`` taken from ``$COGNET_STROKE_SRC``
(the job ``cognet-stroke`` of .github/workflows/steps.yml fetches it at the commit of
``upstream.env`` and sets ``COGNET_STROKE_REQUIRE_UPSTREAM=1``; elsewhere the tests that need it
are skipped when it is absent). The container paths (Docker, Apptainer) and the equivalence with
the previously published image are exercised by that job with ``stroke-step-reftest``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
import warnings
from pathlib import Path

import jsonschema
import nibabel as nib
import numpy as np
import pytest
import yaml

STEP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(STEP_DIR / "src"))

# loaded under a unique name: other steps also have a tests/make_reference.py
_spec = importlib.util.spec_from_file_location(
    "cognet_stroke_make_reference", STEP_DIR / "tests" / "make_reference.py"
)
make_reference = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = make_reference
_spec.loader.exec_module(make_reference)

from cognet_stroke_step import profile as profile_module  # noqa: E402
from cognet_stroke_step import run as step_run  # noqa: E402
from cognet_stroke_step import upstream  # noqa: E402
from cognet_stroke_step.bundle import load_concept, load_concept_maps  # noqa: E402
from cognet_stroke_step.parcels import parcellate_mean  # noqa: E402

from stroke_step_sdk import (  # noqa: E402
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_REJECTED,
    contracts,
    run_step,
)
from stroke_step_sdk.reftest import compare_output  # noqa: E402

CONTRACTS = contracts.contracts_dir()
MANIFEST = STEP_DIR / "step.yaml"
SCHEMA = json.loads((CONTRACTS / "schemas/v1/cognet-profile.schema.json").read_text(encoding="utf-8"))
MEASURES = profile_module.MEASURES

HAVE_UPSTREAM = upstream.cdr_path().is_file()
if not HAVE_UPSTREAM and os.environ.get("COGNET_STROKE_REQUIRE_UPSTREAM") == "1":
    raise RuntimeError(f"the author's src/cdr.py is required but not found at {upstream.cdr_path()}")
needs_upstream = pytest.mark.skipif(
    not HAVE_UPSTREAM,
    reason="the author's COGNET-STROKE src/cdr.py is not available (set COGNET_STROKE_SRC)",
)


def author():
    return upstream.load()


# ----------------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def reference_dir(tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("reference")
    make_reference.generate(dest, CONTRACTS)
    return dest


class Staged:
    def __init__(self, root: Path, reference: Path) -> None:
        self.reference = reference
        self.in_dir, self.out_dir = root / "in", root / "out"
        self.ref_dir = root / "ref"
        for d in (self.in_dir, self.out_dir, root / "tmp", self.ref_dir):
            d.mkdir()
        shutil.copytree(reference / "bundle", self.ref_dir / "concept_maps")
        shutil.copyfile(reference / "lesion_mask.nii.gz", self.in_dir / "lesion_mask.nii.gz")
        shutil.copyfile(
            reference / "fdisconnection_map.nii.gz", self.in_dir / "fdisconnection_map.nii.gz"
        )
        (self.in_dir / "params.json").write_text('{"alpha": 0.05}', encoding="utf-8")
        self.env = {"STEP_CPUS": "1", "TMPDIR": str(root / "tmp")}

    def write(self, port: str, values: np.ndarray) -> None:
        _, affine = make_reference.working_grid(CONTRACTS)
        nib.save(nib.Nifti1Image(values, affine), str(self.in_dir / f"{port}.nii.gz"))

    def read(self, port: str) -> np.ndarray:
        return np.asanyarray(nib.load(str(self.in_dir / f"{port}.nii.gz")).dataobj)

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

    def validate(self) -> list:
        docs = [MANIFEST, self.out_dir / "provenance.json"]
        if (self.out_dir / "error.json").exists():
            docs.append(self.out_dir / "error.json")
        issues = contracts.validator().validate_files(docs)
        return [i.message for i in issues if i.severity == "error"]


@pytest.fixture
def staged(tmp_path, reference_dir) -> Staged:
    return Staged(tmp_path, reference_dir)


def labels_of(reference_dir: Path) -> np.ndarray:
    return np.asanyarray(nib.load(str(reference_dir / "bundle" / "parcellation.nii.gz")).dataobj)


def nan_in_parcels(staged: Staged, reference_dir: Path, parcels) -> np.ndarray:
    """The reference map with every voxel of ``parcels`` set to NaN (as flnm-fast writes a
    parcel whose statistic is undefined)."""
    values = staged.read("fdisconnection_map").astype(np.float32)
    values[np.isin(labels_of(reference_dir), list(parcels))] = np.nan
    staged.write("fdisconnection_map", values)
    return values


# ----------------------------------------------------------------------------- manifest


def test_manifest_declares_standard_codes_and_is_valid():
    from stroke_step_sdk.manifest import load_manifest

    assert load_manifest(MANIFEST).missing_sdk_codes() == []
    issues = contracts.validator().validate_files([MANIFEST])
    assert [i.message for i in issues if i.severity == "error"] == []


def test_upstream_env_names_the_pinned_commit():
    env = dict(
        line.split("=", 1)
        for line in (STEP_DIR / "upstream.env").read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
    )
    assert env["UPSTREAM_REPOSITORY"] == "compneurobilbao/COGNET-STROKE"
    assert len(env["UPSTREAM_COMMIT"]) == 40
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    third = {t["name"]: t["version"] for t in manifest["licence"]["third_party"]}
    assert third["COGNET-STROKE (src/cdr.py)"] == env["UPSTREAM_COMMIT"]


# ----------------------------------------------------------------------------- reference case


@needs_upstream
def test_reference_case_matches_the_independent_expectation(staged):
    assert staged.run() == EXIT_OK
    spec = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))["reference_test"]["expected"]["profile"]
    spec = dict(spec, file=str(staged.reference / "expected" / "cognet_profile.json"))
    assert compare_output(spec, staged.out_dir / "profile.json", STEP_DIR, "cognet_profile") is None
    assert (staged.out_dir / "profile_figure.png").read_bytes()[:4] == b"\x89PNG"
    assert staged.validate() == []


@needs_upstream
def test_profile_content_and_schema(staged):
    assert staged.run() == EXIT_OK
    profile = staged.json("profile.json")
    jsonschema.Draft202012Validator(SCHEMA).validate(profile)
    assert profile["profile_version"] == "1.1"
    assert "excluded_parcels" not in profile  # no NaN in the reference map
    bundle_yaml = yaml.safe_load((staged.ref_dir / "concept_maps" / "bundle.yaml").read_text())
    assert profile["bundle"]["content_digest"] == bundle_yaml["content_digest"]
    assert profile["method"]["n_nulls"] == make_reference.N_NULLS
    by_name = {c["concept"]: c for c in profile["concepts"]}
    # the lesion sits inside motor_left: strongest disconnection, significant after correction
    top = by_name["motor_left"]["disconnection_positive"]
    assert top["significant"] is True
    assert top["score"] > 10 * by_name["visual_early"]["disconnection_positive"]["score"]
    assert by_name["motor_left"]["lesion_overlap"]["score"] > by_name["motor_right"]["lesion_overlap"]["score"]
    domains = {d["domain"]: d for d in profile["domains"]}
    assert domains["motor"]["disconnection_positive"]["significant"] is True
    assert domains["sensory"]["disconnection_positive"]["significant"] is False
    prov = staged.json("provenance.json")
    assert [b["id"] for b in prov["bundles"]] == ["synthetic-concept-maps"]
    software = {s["name"]: s["version"] for s in prov["software"]}
    assert software["COGNET-STROKE"].startswith("git-")
    # the lesion lies wholly inside motor_left: overlap 1.0, which no null can exceed and some
    # nulls reach; the author's strict rule gives 1/n, and the wrapper says so
    assert [w["code"] for w in prov["warnings"]] == ["tied_null_values"]
    assert "motor_left:lesion_overlap" in prov["warnings"][0]["message"]


@needs_upstream
def test_output_is_deterministic(tmp_path, reference_dir):
    outputs = []
    for name in ("a", "b"):
        (tmp_path / name).mkdir()
        run = Staged(tmp_path / name, reference_dir)
        assert run.run() == EXIT_OK
        outputs.append((run.out_dir / "profile.json").read_bytes())
    assert outputs[0] == outputs[1]


@needs_upstream
def test_alpha_changes_significance_only(tmp_path, reference_dir):
    results = {}
    for name, alpha in (("strict", 0.001), ("loose", 0.05)):
        (tmp_path / name).mkdir()
        run = Staged(tmp_path / name, reference_dir)
        (run.in_dir / "params.json").write_text(json.dumps({"alpha": alpha}), encoding="utf-8")
        assert run.run() == EXIT_OK
        results[name] = run.json("profile.json")
    strict = results["strict"]["concepts"][0]["disconnection_positive"]
    loose = results["loose"]["concepts"][0]["disconnection_positive"]
    assert strict["p_value"] == loose["p_value"] and strict["score"] == loose["score"]
    assert (strict["significant"], loose["significant"]) == (False, True)


# ----------------------------------------------------------------------------- the author's code


@needs_upstream
def test_the_scores_are_those_of_the_authors_compute_cdr(staged):
    """Every concept score and p-value of the profile equals a direct call of the author's
    compute_cdr / compute_overlap on the parcel vectors, as the author's workflow makes them."""
    assert staged.run() == EXIT_OK
    profile = staged.json("profile.json")
    maps = load_concept_maps(staged.ref_dir / "concept_maps")
    disc = staged.read("fdisconnection_map").astype(np.float64)
    lesion = staged.read("lesion_mask") > 0
    vectors = {
        "disconnection_positive": parcellate_mean(np.where(disc > 0, disc, 0), maps.labels, maps.ids),
        "disconnection_negative": parcellate_mean(np.where(disc < 0, -disc, 0), maps.labels, maps.ids),
        "lesion_overlap": (parcellate_mean(lesion, maps.labels, maps.ids) > 0).astype(float),
    }
    cdr = author()
    for row in profile["concepts"]:
        mask, nulls = load_concept(maps, row["concept"])
        for measure in MEASURES:
            fn = cdr.compute_overlap if measure == "lesion_overlap" else cdr.compute_cdr
            score, p = fn(mask, vectors[measure], nulls)
            assert row[measure]["score"] == float(score), (row["concept"], measure)
            assert row[measure]["p_value"] == float(p), (row["concept"], measure)


@needs_upstream
def test_the_authors_module_is_loaded_from_the_pinned_checkout():
    """Plumbing: the module the wrapper calls is the file at $COGNET_STROKE_SRC/src/cdr.py
    (the image's copy is compared byte for byte with the pinned commit in CI), loaded under the
    wrapper's own module name, and it is the nanmean version."""
    module = author()
    assert Path(module.__file__).resolve() == upstream.cdr_path().resolve()
    assert module.__name__ == upstream.MODULE_NAME
    source = upstream.cdr_path().read_text(encoding="utf-8")
    assert "def compute_cdr(" in source and "np.nanmean(" in source


# ---- equivalence with the code of step 1.0.0 (its own cognet_runtime/cdr.py, read-only from
# COGNET-STROKE at 56672166; CI fetches it into $COGNET_STROKE_PREVIOUS_SRC, SHA-256 checked)

PREVIOUS_SRC = os.environ.get("COGNET_STROKE_PREVIOUS_SRC")
PREVIOUS_CDR = Path(PREVIOUS_SRC or "/nonexistent") / "runtime" / "src" / "cognet_runtime" / "cdr.py"
if not PREVIOUS_CDR.is_file() and os.environ.get("COGNET_STROKE_REQUIRE_UPSTREAM") == "1":
    raise RuntimeError(f"step 1.0.0's cdr.py is required but not found at {PREVIOUS_CDR}")
needs_previous = pytest.mark.skipif(
    not PREVIOUS_CDR.is_file(), reason="step 1.0.0's cognet_runtime/cdr.py not available"
)


def previous():
    spec = importlib.util.spec_from_file_location("cognet_runtime_cdr_1_0_0", PREVIOUS_CDR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _vectors(rng, n, case):
    mask = np.where(rng.random(n) > 0.7, 1, -1).astype(np.int8)
    values = rng.random(n) * (rng.random(n) > 0.2)
    if case == "empty_inside":
        mask[:] = -1
    elif case == "empty_outside":
        mask[:] = 1
    elif case == "zero_outside":
        values[mask == -1] = 0.0
    elif case == "all_zero":
        values[:] = 0.0
    return mask, values


@needs_upstream
@needs_previous
@pytest.mark.parametrize("case", ["random", "empty_inside", "empty_outside", "zero_outside", "all_zero"])
def test_same_results_as_the_code_of_step_1_0_0(case):
    """On finite vectors the wrapper (author's code plus the wrapper's handling) gives what
    1.0.0's own implementation gave: the same score and flag, and the same p-value, except
    where 1.0.0 reported 1/n_nulls for a degenerate null distribution (now null, with a
    warning) or NaN for an empty mask (now null)."""
    old = previous()
    rng = np.random.default_rng(11)
    for _ in range(10):
        n = 60
        mask, values = _vectors(rng, n, case)
        nulls = np.where(rng.random((200, n)) > 0.7, 1, -1).astype(np.int8)
        lesion = (rng.random(n) > 0.9).astype(float)
        lesion[0] = 1.0
        diagnostics = profile_module.Diagnostics()
        vectors = {"disconnection_positive": values, "disconnection_negative": values, "lesion_overlap": lesion}
        row = profile_module._concept_row("c", "d", mask, nulls, vectors, author(), diagnostics)
        degenerate = {m for _c, m in diagnostics.degenerate}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            expected = {
                "disconnection_positive": (*old.compute_cdr(mask, values, nulls), old.cdr_flag(mask, values)),
                "lesion_overlap": (*old.compute_overlap(mask, lesion, nulls), None),
            }
        for measure, (score, p, flag) in expected.items():
            got = row[measure]
            if np.isnan(score):
                assert got["score"] is None and got["p_value"] is None
            else:
                assert got["score"] == score
                assert got["p_value"] == (None if measure in degenerate else p)
            assert got.get("flag") == flag


@needs_upstream
def test_an_undefined_score_has_no_p_value():
    """The author's code gives NaN with p = 1 / n_nulls when no parcel is inside the concept;
    the wrapper reports a null score, a null p-value and flag empty_mask, never p = 1 / n."""
    mask = np.array([-1, -1, -1, -1], dtype=np.int8)
    values = np.array([1.0, 2.0, 3.0, 4.0])
    nulls = np.array([[1, -1, -1, -1], [-1, 1, -1, -1]], dtype=np.int8)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        score, p = author().compute_cdr(mask, values, nulls)
    assert np.isnan(score) and p == 0.5  # the author's behaviour, documented in profile.py
    row = profile_module._concept_row("c", "d", mask, nulls, {m: values for m in MEASURES}, author())
    assert row["disconnection_positive"] == {"score": None, "p_value": None, "flag": "empty_mask"}


@needs_upstream
def test_nan_parcels_are_left_out_by_the_authors_nanmean():
    """A NaN parcel counts neither inside nor outside, for the observed mask and every null."""
    cdr = author()
    mask = np.array([1, 1, -1, -1, -1], dtype=np.int8)
    values = np.array([4.0, np.nan, 1.0, np.nan, 1.0])
    nulls = np.array([[1, -1, 1, -1, -1], [-1, 1, 1, -1, -1]], dtype=np.int8)
    score, p = cdr.compute_cdr(mask, values, nulls)
    assert score == pytest.approx(4.0)  # 4 / mean(1, 1)
    # null 1: inside {4, 1} = 2.5, outside {nan, nan, 1} = 1 -> 2.5 (not a hit); null 2: inside
    # {nan, 1} = 1, outside {4, nan, 1} = 2.5 -> 0.4: no hit -> p = 1 / 2
    assert p == pytest.approx(0.5)
    row = profile_module._concept_row("c", "d", mask, nulls, {m: values for m in MEASURES}, cdr)
    assert row["disconnection_positive"]["score"] == pytest.approx(4.0)
    assert "flag" not in row["disconnection_positive"]
    # every inside parcel undefined: no score
    undefined_inside = np.array([np.nan, np.nan, 1.0, 1.0, 1.0])
    row = profile_module._concept_row("c", "d", mask, nulls, {m: undefined_inside for m in MEASURES}, cdr)
    assert row["disconnection_negative"]["score"] is None
    assert row["disconnection_negative"]["flag"] == "empty_mask"


# ----------------------------------------------------------------------------- NaN in the map


@needs_upstream
def test_parcels_without_value_are_excluded_and_reported(staged, reference_dir, tmp_path):
    labels = labels_of(reference_dir)
    ids = np.unique(labels[labels > 0])
    excluded = set(ids[::7].tolist())
    nan_in_parcels(staged, reference_dir, excluded)
    assert staged.run() == EXIT_OK
    assert staged.validate() == []
    profile = staged.json("profile.json")
    jsonschema.Draft202012Validator(SCHEMA).validate(profile)
    assert profile["excluded_parcels"] == {
        "count": len(excluded),
        "fraction": len(excluded) / ids.size,
        "reason": "undefined_in_disconnection_map",
    }
    prov = staged.json("provenance.json")
    codes = [w["code"] for w in prov["warnings"]]
    assert codes[0] == "parcels_excluded"
    assert set(codes) <= {"parcels_excluded", "undefined_null_maps", "tied_null_values", "degenerate_null_distribution"}

    # independent check: the naive implementation of the reference generator on the vectors
    # with the excluded parcels removed (masks and nulls restricted the same way)
    disc = staged.read("fdisconnection_map").astype(np.float64)
    keep = ~np.isin(ids, list(excluded))
    pos = [float(np.mean(np.where(disc > 0, disc, 0.0)[labels == i])) for i in ids[keep]]
    maps = load_concept_maps(staged.ref_dir / "concept_maps")
    for row in profile["concepts"]:
        mask, nulls = load_concept(maps, row["concept"])
        m, nn = mask[keep].tolist(), nulls[:, keep].tolist()
        score = make_reference._naive_cdr(m, pos)
        p = make_reference._p(score, [make_reference._naive_cdr(n, pos) for n in nn])
        assert row["disconnection_positive"]["score"] == pytest.approx(score, rel=1e-9)
        assert row["disconnection_positive"]["p_value"] == pytest.approx(p)

    # the lesion overlap does not depend on the disconnection map
    (tmp_path / "plain").mkdir()
    plain = Staged(tmp_path / "plain", reference_dir)
    assert plain.run() == EXIT_OK
    for a, b in zip(profile["concepts"], plain.json("profile.json")["concepts"]):
        assert a["lesion_overlap"] == b["lesion_overlap"]


@needs_upstream
def test_nan_voxels_inside_a_parcel_use_the_defined_voxels(staged, reference_dir):
    """Only some voxels of a parcel undefined: the parcel keeps the mean of the others and is
    not excluded."""
    labels = labels_of(reference_dir)
    values = staged.read("fdisconnection_map").astype(np.float32)
    target = int(labels[make_reference.LESION_CENTRE])
    voxels = np.argwhere(labels == target)
    values[tuple(voxels[: len(voxels) // 2].T)] = np.nan
    staged.write("fdisconnection_map", values)
    assert staged.run() == EXIT_OK
    assert "excluded_parcels" not in staged.json("profile.json")
    ids = np.unique(labels[labels > 0])
    positive, _ = profile_module.split_disconnection(values)
    means = parcellate_mean(positive, labels, ids)
    defined = values[labels == target]
    defined = defined[~np.isnan(defined)].astype(np.float64)
    assert means[ids.tolist().index(target)] == pytest.approx(float(np.mean(np.maximum(defined, 0))))


def test_split_keeps_undefined_voxels_undefined():
    pos, neg = profile_module.split_disconnection(np.array([2.0, -3.0, np.nan, 0.0]))
    np.testing.assert_array_equal(pos, [2.0, 0.0, np.nan, 0.0])
    np.testing.assert_array_equal(neg, [0.0, 3.0, np.nan, 0.0])


def test_parcellate_mean_without_nan_is_the_plain_mean():
    rng = np.random.default_rng(3)
    labels = rng.integers(0, 6, size=(5, 6, 7))
    data = rng.normal(size=labels.shape)
    ids = np.arange(1, 6)
    expected = [data[labels == i].mean() for i in ids]
    np.testing.assert_allclose(parcellate_mean(data, labels, ids), expected, rtol=1e-12)
    data[labels == 3] = np.nan
    out = parcellate_mean(data, labels, ids)
    assert np.isnan(out[2]) and np.isfinite(np.delete(out, 2)).all()


def test_excluded_parcels_is_absent_without_nan():
    vectors = {m: np.ones(4) for m in MEASURES}
    assert profile_module.excluded_parcels(vectors, 4) is None
    vectors["disconnection_positive"] = np.array([1.0, np.nan, 1.0, np.nan])
    vectors["disconnection_negative"] = np.array([1.0, np.nan, 1.0, np.nan])
    assert profile_module.excluded_parcels(vectors, 4) == {
        "count": 2,
        "fraction": 0.5,
        "reason": "undefined_in_disconnection_map",
    }


@needs_upstream
def test_infinite_values_are_rejected(staged):
    values = staged.read("fdisconnection_map").astype(np.float32)
    values[40, 50, 40] = np.inf
    staged.write("fdisconnection_map", values)
    assert staged.run() == EXIT_REJECTED
    err = staged.json("error.json")
    assert (err["category"], err["code"], err["port"]) == (
        "input_rejected",
        "disconnection_not_finite",
        "fdisconnection_map",
    )
    assert staged.validate() == []


@needs_upstream
def test_an_all_nan_map_is_rejected(staged):
    staged.write("fdisconnection_map", np.full(staged.read("lesion_mask").shape, np.nan, np.float32))
    assert staged.run() == EXIT_REJECTED
    err = staged.json("error.json")
    assert (err["category"], err["code"]) == ("input_rejected", "disconnection_map_undefined")
    assert staged.validate() == []


@needs_upstream
def test_a_map_undefined_in_every_parcel_is_rejected(staged, reference_dir):
    labels = labels_of(reference_dir)
    values = np.zeros(labels.shape, np.float32)
    values[labels > 0] = np.nan
    values[0, 0, 0] = 1.0  # defined outside the parcellation only
    staged.write("fdisconnection_map", values)
    assert staged.run() == EXIT_REJECTED
    err = staged.json("error.json")
    assert (err["category"], err["code"]) == ("input_rejected", "disconnection_map_undefined")
    assert staged.validate() == []


# ----------------------------------------------------------------------------- other rejections


@needs_upstream
def test_empty_lesion_is_rejected(staged):
    staged.write("lesion_mask", np.zeros(staged.read("lesion_mask").shape, np.uint8))
    assert staged.run() == EXIT_REJECTED
    err = staged.json("error.json")
    assert (err["category"], err["code"], err["port"]) == ("input_rejected", "mask_empty", "lesion_mask")
    assert staged.validate() == []


@needs_upstream
def test_zero_disconnection_map_fails_qc(staged):
    values = np.zeros(staged.read("lesion_mask").shape, np.float32)
    values[0, 0, 0] = np.nan  # zero wherever it is defined
    staged.write("fdisconnection_map", values)
    assert staged.run() == EXIT_REJECTED
    err = staged.json("error.json")
    assert (err["category"], err["code"]) == ("qc_failed", "disconnection_map_empty")
    assert staged.validate() == []


@needs_upstream
def test_lesion_outside_the_parcellation_fails_qc(staged):
    values = np.zeros(staged.read("lesion_mask").shape, np.uint8)
    values[0:3, 0:3, 0:3] = 1  # corner of the grid, outside the synthetic brain
    staged.write("lesion_mask", values)
    assert staged.run() == EXIT_REJECTED
    err = staged.json("error.json")
    assert (err["category"], err["code"]) == ("qc_failed", "lesion_outside_parcellation")
    assert staged.validate() == []


@needs_upstream
def test_invalid_bundle_is_an_internal_failure(staged):
    (staged.ref_dir / "concept_maps" / "concepts" / "attention.npz").unlink()
    assert staged.run() == EXIT_INTERNAL
    assert staged.json("error.json")["code"] == "bundle_layout_invalid"


def test_missing_author_code_is_an_internal_failure(staged, tmp_path, monkeypatch):
    monkeypatch.setenv("COGNET_STROKE_SRC", str(tmp_path / "nowhere"))
    upstream._load.cache_clear()
    try:
        assert staged.run() == EXIT_INTERNAL
    finally:
        upstream._load.cache_clear()
    assert staged.json("error.json")["code"] == "upstream_unavailable"


@needs_upstream
def test_unknown_parameter_and_missing_input(staged):
    (staged.in_dir / "params.json").write_text('{"nope": 1}', encoding="utf-8")
    assert staged.run() == EXIT_REJECTED
    assert staged.json("error.json")["code"] == "invalid_parameters"
    (staged.in_dir / "params.json").write_text("{}", encoding="utf-8")
    (staged.in_dir / "fdisconnection_map.nii.gz").unlink()
    shutil.rmtree(staged.out_dir)
    staged.out_dir.mkdir()
    assert staged.run() == EXIT_REJECTED
    assert staged.json("error.json")["code"] == "input_missing"


# ----------------------------------------------------------------------------- the parcellation


@needs_upstream
def test_the_parcel_count_comes_from_the_bundle(staged, reference_dir):
    """A bundle with another parcellation (pairs of parcels merged) works without any change:
    the step takes the parcels from the bundle's parcellation."""
    bundle = staged.ref_dir / "concept_maps"
    labels = labels_of(reference_dir)
    merged = ((labels.astype(np.int32) + 1) // 2).astype(np.int16)
    _, affine = make_reference.working_grid(CONTRACTS)
    nib.save(nib.Nifti1Image(merged, affine), str(bundle / "parcellation.nii.gz"))
    n = int(merged.max())
    cent = make_reference.centroids(merged)
    rng = np.random.default_rng(1)
    for concept, (_domain, centre, radius) in make_reference.CONCEPTS.items():
        mask = make_reference.ball_mask(cent, centre, radius)
        nulls = np.stack(
            [make_reference.ball_mask(cent, cent[rng.integers(n)], radius) for _ in range(50)]
        )
        make_reference.write_npz(bundle / "concepts" / f"{concept}.npz", mask=mask, nulls=nulls)
    doc = yaml.safe_load((bundle / "bundle.yaml").read_text(encoding="utf-8"))
    files = [
        {
            "path": p.relative_to(bundle).as_posix(),
            "sha256": make_reference.sha256(p),
            "size_bytes": p.stat().st_size,
        }
        for p in sorted(bundle.rglob("*"))
        if p.is_file() and p.name != "bundle.yaml"
    ]
    doc.update(files=files, content_digest=make_reference.content_digest(files))
    (bundle / "bundle.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    assert staged.run() == EXIT_OK
    profile = staged.json("profile.json")
    assert profile["method"]["n_parcels"] == n
    assert n != int(labels.max())
    assert profile["method"]["n_nulls"] == 50


# ----------------------------------------------------------------------------- null distributions


@needs_upstream
def test_a_map_without_positive_values_is_not_significant_anywhere(staged):
    """Only negative values: the positive part is zero everywhere, so every concept's positive
    score is 0 and so is every null. The author's strict rule gives p = 1/n for all of them,
    which 1.0.0 reported as significant; the wrapper reports the p-value as null."""
    values = staged.read("fdisconnection_map").astype(np.float32)
    staged.write("fdisconnection_map", -np.abs(values))
    assert staged.run() == EXIT_OK
    assert staged.validate() == []
    profile = staged.json("profile.json")
    for row in profile["concepts"]:
        result = row["disconnection_positive"]
        assert result["score"] == 0.0
        assert result["p_value"] is None and result["significant"] is False
        assert result["flag"] == "zero_outside_mean"
    for row in profile["domains"]:
        assert row["disconnection_positive"]["significant"] is False
    codes = [w["code"] for w in staged.json("provenance.json")["warnings"]]
    assert "degenerate_null_distribution" in codes


@needs_upstream
def test_degenerate_undefined_and_tied_nulls_are_reported():
    cdr = author()
    # all nulls equal (zero spread): p would be 1/n whatever the data
    mask = np.array([1, -1, -1, -1], dtype=np.int8)
    nulls = np.array([[1, -1, -1, -1]] * 4, dtype=np.int8)
    values = np.array([2.0, 1.0, 1.0, 1.0])
    diagnostics = profile_module.Diagnostics(n_nulls=4)
    row = profile_module._concept_row("c", "d", mask, nulls, {m: values for m in MEASURES}, cdr, diagnostics)
    assert row["disconnection_positive"]["p_value"] is None
    assert ("c", "disconnection_positive") in diagnostics.degenerate

    # some nulls undefined (their only inside parcel is NaN): counted, p kept (author's denominator)
    values = np.array([3.0, np.nan, 1.0, 2.0, 1.0])
    mask = np.array([1, -1, -1, -1, -1], dtype=np.int8)
    nulls = np.array([[-1, 1, -1, -1, -1], [-1, -1, 1, -1, -1], [-1, -1, -1, 1, -1]], dtype=np.int8)
    diagnostics = profile_module.Diagnostics(n_nulls=3)
    row = profile_module._concept_row("c", "d", mask, nulls, {m: values for m in MEASURES}, cdr, diagnostics)
    score, p = cdr.compute_cdr(mask, values, nulls)
    assert row["disconnection_positive"]["p_value"] == p
    assert ("c", "disconnection_positive", 1) in diagnostics.undefined_nulls

    # ties: nothing larger, some equal
    values = np.array([1.0, 0.0, 1.0, 0.0])
    mask = np.array([1, -1, -1, -1], dtype=np.int8)
    nulls = np.array([[-1, -1, 1, -1], [-1, 1, -1, -1], [-1, -1, -1, 1]], dtype=np.int8)
    diagnostics = profile_module.Diagnostics(n_nulls=3)
    row = profile_module._concept_row(
        "c", "d", mask, nulls, {m: np.array([1.0, 0.0, 1.0, 0.0]) for m in MEASURES}, cdr, diagnostics
    )
    assert row["lesion_overlap"]["p_value"] == pytest.approx(1 / 3)
    assert ("c", "lesion_overlap", 1) in diagnostics.ties
    codes = [code for code, _message in diagnostics.warnings()]
    assert "tied_null_values" in codes
    assert all(len(message) <= 500 for _code, message in diagnostics.warnings())


def test_warning_messages_stay_within_the_provenance_limit():
    diagnostics = profile_module.Diagnostics(n_nulls=5000)
    diagnostics.degenerate = [(f"concept_{i}", "disconnection_positive") for i in range(200)]
    ((code, message),) = diagnostics.warnings()
    assert code == "degenerate_null_distribution" and len(message) <= 500
    assert message.endswith("more")


# ----------------------------------------------------------------------------- grids


@needs_upstream
def test_partly_defined_parcels_are_reported(staged, reference_dir):
    labels = labels_of(reference_dir)
    values = staged.read("fdisconnection_map").astype(np.float32)
    target = int(labels[make_reference.LESION_CENTRE])
    voxels = np.argwhere(labels == target)
    values[tuple(voxels[: len(voxels) // 4].T)] = np.nan
    staged.write("fdisconnection_map", values)
    assert staged.run() == EXIT_OK
    warnings_ = {w["code"]: w["message"] for w in staged.json("provenance.json")["warnings"]}
    assert "1 of" in warnings_["parcels_partly_defined"]
    assert "lowest share 7" in warnings_["parcels_partly_defined"]  # 3/4 of the voxels defined


@needs_upstream
def test_a_bundle_parcellation_off_the_working_grid_is_refused(staged):
    bundle = staged.ref_dir / "concept_maps"
    img = nib.load(str(bundle / "parcellation.nii.gz"))
    shifted = img.affine.copy()
    shifted[0, 3] += 2.0
    nib.save(nib.Nifti1Image(np.asanyarray(img.dataobj), shifted), str(bundle / "parcellation.nii.gz"))
    assert staged.run() == EXIT_INTERNAL
    err = staged.json("error.json")
    assert err["code"] == "bundle_layout_invalid"
    assert "working grid" in err["details"]["reason"]


@needs_upstream
def test_a_disconnection_map_off_the_working_grid_is_rejected(staged):
    values = staged.read("fdisconnection_map").astype(np.float32)
    _, affine = make_reference.working_grid(CONTRACTS)
    shifted = affine.copy()
    shifted[1, 3] += 4.0
    nib.save(nib.Nifti1Image(values, shifted), str(staged.in_dir / "fdisconnection_map.nii.gz"))
    assert staged.run() == EXIT_REJECTED
    assert staged.json("error.json")["code"] == "input_format_invalid"
