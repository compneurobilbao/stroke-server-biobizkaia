"""Tests of the flnm-fast step wrapper on the synthetic reference dataset.

Tests marked ``engine`` run the real, unchanged engine script of the base image
(``compneurobilbaolab/flnm-fast``). They need pandas and scipy and the script, which is not in
this repository: point ``FLNM_FAST_SCRIPT`` at a copy extracted from the image
(``docker run --rm --entrypoint cat <image> /opt/lesion_zmaps/create_lesion_zmaps.py``).
Without it they are skipped, unless ``FLNM_FAST_REQUIRE_ENGINE`` is set (the container CI job
does), in which case a missing engine fails the run. The other tests use a fake engine that
only writes the files the wrapper reads. Container paths are exercised by
.github/workflows/steps.yml.
"""

from __future__ import annotations

import csv
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
    "flnm_fast_make_reference", STEP_DIR / "tests" / "make_reference.py"
)
make_reference = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = make_reference
_spec.loader.exec_module(make_reference)

from flnm_fast_step import run as step_run  # noqa: E402
from flnm_fast_step import step as step_module  # noqa: E402

from stroke_step_sdk import (  # noqa: E402
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_REJECTED,
    contracts,
    run_step,
)

MANIFEST = STEP_DIR / "step.yaml"
REGISTRY = contracts.contracts_dir() / "artefact-types.yaml"
SHAPE, AFFINE = make_reference.working_grid(REGISTRY)
MAP = "fdisconnection_map.nii.gz"


def _find_engine() -> Path | None:
    env = os.environ.get("FLNM_FAST_SCRIPT")
    path = Path(env) if env else step_module.ENGINE_SCRIPT
    if not path.is_file():
        return None
    return path if importlib.util.find_spec("pandas") and importlib.util.find_spec("scipy") else None


ENGINE = _find_engine()
ENGINE_VERSION = "2.1.0"  # the version of the pinned base image
if os.environ.get("FLNM_FAST_REQUIRE_ENGINE"):
    assert ENGINE is not None, "FLNM_FAST_REQUIRE_ENGINE is set but the engine is not available"
engine = pytest.mark.skipif(ENGINE is None, reason="needs the engine script, pandas and scipy")


@pytest.fixture(scope="session")
def reference(tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("flnm-fast-reference")
    make_reference.generate(dest, REGISTRY)
    return dest


@pytest.fixture
def use_engine(monkeypatch):
    monkeypatch.setattr(step_module, "ENGINE_SCRIPT", ENGINE)


def execute(
    tmp_path: Path,
    mask: Path,
    bundle: Path,
    params: dict,
    *,
    with_mask: bool = True,
) -> tuple[int, Path]:
    in_dir, out_dir, ref = tmp_path / "in", tmp_path / "out", tmp_path / "ref"
    for d in (in_dir, out_dir, tmp_path / "tmp", ref):
        d.mkdir()
    shutil.copytree(bundle, ref / "normative")
    if with_mask:
        shutil.copyfile(mask, in_dir / "lesion_mask.nii.gz")
    (in_dir / "params.json").write_text(json.dumps(params), encoding="utf-8")
    code = run_step(
        step_run,
        in_dir,
        out_dir,
        manifest_path=MANIFEST,
        ref_dir=ref,
        environ={"STEP_CPUS": "1", "TMPDIR": str(tmp_path / "tmp")},
    )
    return code, out_dir


def error_code(out: Path) -> str:
    return json.loads((out / "error.json").read_text(encoding="utf-8"))["code"]


def zmap(path: Path) -> np.ndarray:
    return np.asanyarray(nib.load(str(path)).dataobj)


def assert_contract_valid(out: Path, *extra: str) -> None:
    issues = contracts.validator().validate_files(
        [MANIFEST, out / "provenance.json", *(out / e for e in extra)]
    )
    assert [i.message for i in issues if i.severity == "error"] == []


PARAMS: dict = {}  # the step has no parameters


# --- manifest, bundle and reference data (no engine needed) ---------------------------------


def test_manifest_is_valid_contract_1_2_real_step():
    issues = contracts.validator().validate_files([MANIFEST])
    assert [i.message for i in issues if i.severity == "error"] == []
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert doc["contract_version"] == "1.2"
    assert doc["provides"] == "functional-disconnection"
    assert doc.get("stub", False) is False
    assert doc["outputs"][0]["type"] == "fdisconnection_map"
    assert doc["reference_test"]["generator"] == "tests/make_reference.py"


def test_upstream_block_matches_the_dockerfile_and_the_third_party_list():
    """F1-23 worked example: the block names the engine image pinned in the Dockerfile."""
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    up = doc["upstream"]
    dockerfile = (MANIFEST.parent / "Dockerfile").read_text(encoding="utf-8")
    base = next(x for x in dockerfile.splitlines() if x.startswith("FROM ")).split()[1]
    assert up["image"] == f"docker.io/{base}"
    assert up["licence"] == "MIT" and up["homepage"].startswith("https://")
    assert up["version"] == ENGINE_VERSION
    third = {t["name"]: t["spdx"] for t in doc["licence"]["third_party"]}
    assert third[up["name"]] == up["licence"]


def test_output_type_is_the_one_of_the_stub():
    stub = yaml.safe_load(
        (REPO / "steps/stubs/fdisconnection-stub/step.yaml").read_text(encoding="utf-8")
    )
    real = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    pick = lambda m: [(o["name"], o["type"], o["required"]) for o in m["outputs"]]  # noqa: E731
    assert pick(real) == pick(stub)
    assert [i["name"] for i in real["inputs"]] == [i["name"] for i in stub["inputs"]]


def test_reference_bundle_is_a_valid_bundle(reference):
    issues = contracts.validator().validate_files([reference / "bundle" / "bundle.yaml"])
    assert [i.message for i in issues if i.severity == "error"] == []
    doc = yaml.safe_load((reference / "bundle" / "bundle.yaml").read_text(encoding="utf-8"))
    assert doc["synthetic"] is True and doc["type"] == "normative-fc"
    for entry in doc["files"]:
        data = (reference / "bundle" / entry["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == entry["sha256"]


def test_reference_data_is_byte_identical_on_regeneration(reference, tmp_path):
    make_reference.generate(tmp_path, REGISTRY)
    files = sorted(p.relative_to(reference) for p in reference.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    for rel in files:
        assert (reference / rel).read_bytes() == (tmp_path / rel).read_bytes(), rel


def test_reference_cohort_has_no_age_or_sex(reference):
    """Like the real cohorts: identifiers only, so nothing in the step may need age or sex."""
    with (reference / "bundle" / "normative_subjects.csv").open(newline="") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames == ["subject_index", "subject_id"]
        rows = list(reader)
    assert sorted(int(r["subject_index"]) for r in rows) == list(range(make_reference.N_SUBJECTS))
    assert [int(r["subject_index"]) for r in rows] != list(range(make_reference.N_SUBJECTS))


def test_independent_statistics_match_scipy():
    stats = pytest.importorskip("scipy.stats")
    for df in (2, 19, 59):
        for t in (-12.0, -2.5, -0.1, 0.0, 0.3, 4.0, 9.5):
            p = float(2.0 * stats.t.sf(abs(t), df))
            assert make_reference.two_sided_p(t, df) == pytest.approx(p, rel=1e-9, abs=1e-300)
            z = float(np.sign(t) * stats.norm.isf(max(p, np.finfo(float).tiny) / 2.0))
            assert make_reference.signed_z(t, df) == pytest.approx(z, rel=1e-9, abs=1e-12)


def test_step_has_no_parameters_and_only_the_two_lesion_rejections():
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert doc["parameters"]["properties"] == {}
    assert doc["parameters"]["additionalProperties"] is False
    codes = {e["code"] for e in doc["errors"]}
    assert codes == {"mask_empty", "lesion_outside_parcellation"}


def _bundle_copy(reference, tmp_path, name="b") -> Path:
    bundle = tmp_path / name
    shutil.copytree(reference / "bundle", bundle)
    return bundle


def _set_metadata(bundle: Path, **values) -> None:
    doc = yaml.safe_load((bundle / "bundle.yaml").read_text(encoding="utf-8"))
    doc["metadata"].pop("parcellation", None)
    doc["metadata"].update({k: v for k, v in values.items() if v is not None})
    (bundle / "bundle.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_parcellation_comes_from_the_bundle_metadata(reference, tmp_path):
    bundle = _bundle_copy(reference, tmp_path)
    assert step_module.parcellation_path(bundle) == (
        bundle / "standard" / "synthetic_blocks27_2mm.nii.gz"
    ).resolve()
    # another file name, also outside standard/: the metadata decides, not the name or place
    (bundle / "standard" / "synthetic_blocks27_2mm.nii.gz").rename(bundle / "firsT_2566.nii.gz")
    _set_metadata(bundle, parcellation="firsT_2566.nii.gz")
    assert step_module.parcellation_path(bundle) == (bundle / "firsT_2566.nii.gz").resolve()


def test_single_file_under_standard_is_the_fallback(reference, tmp_path):
    bundle = _bundle_copy(reference, tmp_path)
    _set_metadata(bundle)  # no metadata.parcellation
    assert step_module.parcellation_path(bundle).name == "synthetic_blocks27_2mm.nii.gz"
    (bundle / "bundle.yaml").unlink()  # not even a manifest: still the single file
    assert step_module.parcellation_path(bundle).name == "synthetic_blocks27_2mm.nii.gz"


@pytest.mark.parametrize(
    "case",
    ["two_files_no_metadata", "no_file", "metadata_missing_file", "metadata_escapes", "not_nifti"],
)
def test_ambiguous_or_invalid_parcellation_is_refused(reference, tmp_path, case):
    from stroke_step_sdk import InternalFailure

    bundle = _bundle_copy(reference, tmp_path)
    parcel = bundle / "standard" / "synthetic_blocks27_2mm.nii.gz"
    if case == "two_files_no_metadata":
        shutil.copyfile(parcel, bundle / "standard" / "other.nii.gz")
        _set_metadata(bundle)
    elif case == "no_file":
        parcel.unlink()
        _set_metadata(bundle)
    elif case == "metadata_missing_file":
        _set_metadata(bundle, parcellation="standard/absent.nii.gz")
    elif case == "metadata_escapes":
        shutil.copyfile(parcel, tmp_path / "outside.nii.gz")
        _set_metadata(bundle, parcellation="../outside.nii.gz")
    else:
        _set_metadata(bundle, parcellation="standard/synthetic_blocks27_2mm.txt")
    with pytest.raises(InternalFailure) as info:
        step_module.parcellation_path(bundle)
    assert info.value.code == "bundle_layout_unsupported"


def test_metadata_resolves_ambiguity_between_two_parcellations(reference, tmp_path):
    bundle = _bundle_copy(reference, tmp_path)
    shutil.copyfile(
        bundle / "standard" / "synthetic_blocks27_2mm.nii.gz", bundle / "standard" / "other.nii.gz"
    )
    assert step_module.parcellation_path(bundle).name == "synthetic_blocks27_2mm.nii.gz"


# --- rejections that do not need the engine --------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [{"sex": "female"}, {"age_band": "60-64"}, {"control_count": 16}, {"unknown": 1}],
)
def test_removed_and_unknown_parameters_use_the_reserved_code(reference, tmp_path, params):
    """sex, age_band and control_count were removed in 3.0.0: they are refused like any other."""
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", params)
    assert result == EXIT_REJECTED
    assert error_code(out) == "invalid_parameters"


def test_missing_lesion_mask_uses_the_reserved_code(reference, tmp_path):
    result, out = execute(
        tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS, with_mask=False
    )
    assert result == EXIT_REJECTED
    assert error_code(out) == "input_missing"


def test_bundle_without_the_expected_layout_is_an_internal_failure(reference, tmp_path):
    broken = tmp_path / "broken-bundle"
    shutil.copytree(reference / "bundle", broken)
    (broken / "normative_subjects.csv").unlink()
    (tmp_path / "run").mkdir()
    result, out = execute(tmp_path / "run", reference / "lesion_mask.nii.gz", broken, PARAMS)
    assert result == EXIT_INTERNAL
    assert error_code(out) == "bundle_layout_unsupported"
    assert not (out / MAP).exists()


def test_parcellation_off_the_working_grid_is_an_internal_failure(reference, tmp_path):
    broken = tmp_path / "broken-bundle"
    shutil.copytree(reference / "bundle", broken)
    off = AFFINE.copy()
    off[:3, 3] += 2.0
    make_reference.write_nifti(
        broken / make_reference.PARCELLATION_FILE, make_reference.parcellation(SHAPE), off
    )
    (tmp_path / "run").mkdir()
    result, out = execute(tmp_path / "run", reference / "lesion_mask.nii.gz", broken, PARAMS)
    assert result == EXIT_INTERNAL
    assert error_code(out) == "bundle_layout_unsupported"


def _rewrite_table(bundle: Path, mutate) -> None:
    path = bundle / "normative_subjects.csv"
    with path.open(newline="") as fh:
        rows = list(csv.reader(fh))
    rows = mutate(rows)
    with path.open("w", newline="") as fh:
        csv.writer(fh, lineterminator="\n").writerows(rows)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows[:-1],  # a participant of the array without a row
        lambda rows: rows + [rows[-1]],  # duplicate subject_index
        lambda rows: [["subject_id"]] + [[r[1]] for r in rows[1:]],  # no subject_index
        lambda rows: [rows[0]] + [["x", *r[1:]] for r in rows[1:]],  # not integers
    ],
    ids=["missing-row", "duplicate", "no-index-column", "non-integer"],
)
def test_normative_table_that_does_not_cover_the_array_is_an_internal_failure(
    reference, tmp_path, mutate
):
    bundle = _bundle_copy(reference, tmp_path)
    _rewrite_table(bundle, mutate)
    (tmp_path / "run").mkdir()
    result, out = execute(tmp_path / "run", reference / "lesion_mask.nii.gz", bundle, PARAMS)
    assert result == EXIT_INTERNAL
    assert error_code(out) == "bundle_layout_unsupported"
    assert not (out / MAP).exists()


# --- mapping of engine failures, with a fake engine ------------------------------------------


def fake_engine(tmp_path: Path, monkeypatch, body: str) -> None:
    script = tmp_path / "fake_engine.py"
    script.write_text(
        textwrap.dedent(
            """
            import argparse, csv, sys
            from pathlib import Path
            ap = argparse.ArgumentParser()
            for name in ("participants", "participant-id", "lesion-dir", "parcellation",
                         "normative-subjects", "fc", "out-dir"):
                ap.add_argument("--" + name)
            a = ap.parse_args()
            out = Path(a.out_dir)
            out.mkdir(parents=True, exist_ok=True)
            """
        )
        + textwrap.dedent(body),
        encoding="utf-8",
    )
    monkeypatch.setattr(step_module, "ENGINE_SCRIPT", script)


FAIL = """
with (out / "failures.csv").open("w", newline="") as f:
    w = csv.writer(f)
    w.writerow(["participant_id", "error"])
    w.writerow(["case", {text!r}])
"""


@pytest.mark.parametrize(
    ("text", "code", "category"),
    [
        ("Empty lesion mask after thresholding: /tmp/x/case.nii.gz", "mask_empty", "input_rejected"),
    ],
)
def test_known_engine_failures_map_to_declared_codes(
    reference, tmp_path, monkeypatch, text, code, category
):
    fake_engine(tmp_path, monkeypatch, FAIL.format(text=text))
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    assert result == EXIT_REJECTED
    error = json.loads((out / "error.json").read_text(encoding="utf-8"))
    assert (error["code"], error["category"]) == (code, category)
    assert "/tmp" not in error["message"] and "case.nii" not in error["message"]
    assert not (out / MAP).exists()
    assert_contract_valid(out, "error.json")


def test_no_overlap_failure_maps_to_outside_parcellation(reference, tmp_path, monkeypatch):
    fake_engine(
        tmp_path, monkeypatch, FAIL.format(text="Lesion does not overlap any parcellation ROI")
    )
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    assert result == EXIT_REJECTED
    assert error_code(out) == "lesion_outside_parcellation"
    assert_contract_valid(out, "error.json")


def test_unknown_engine_failure_is_internal_and_leaks_no_path(reference, tmp_path, monkeypatch):
    fake_engine(tmp_path, monkeypatch, FAIL.format(text="boom in /data/secret-dir/x.nii.gz"))
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    assert result == EXIT_INTERNAL
    error = json.loads((out / "error.json").read_text(encoding="utf-8"))
    assert error["code"] == "engine_failed"
    assert "secret-dir" not in json.dumps(error) and "<path>" in json.dumps(error)


def test_engine_crash_is_internal(reference, tmp_path, monkeypatch):
    fake_engine(tmp_path, monkeypatch, "print('Error: nope', file=sys.stderr); sys.exit(1)\n")
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    assert result == EXIT_INTERNAL
    assert error_code(out) == "engine_failed"


def test_missing_engine_output_is_internal(reference, tmp_path, monkeypatch):
    fake_engine(tmp_path, monkeypatch, "pass\n")
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    assert result == EXIT_INTERNAL
    assert error_code(out) == "engine_output_missing"


def test_engine_command_line_carries_the_case_and_the_bundle(reference, tmp_path, monkeypatch):
    """No age or sex row, no --match-age-sex, no --control-count: the engine would refuse them
    (the fake engine's parser has no such options)."""
    seen = tmp_path / "seen.json"
    fake_engine(
        tmp_path,
        monkeypatch,
        f"""
import json
rows = list(csv.reader(open(a.participants), delimiter="\\t"))
lesion = Path(a.lesion_dir) / (a.participant_id + ".nii.gz")
Path({str(seen)!r}).write_text(json.dumps({{
    "rows": rows, "lesion_exists": lesion.is_file(),
    "fc": Path(a.fc).name, "subjects": Path(a.normative_subjects).name,
    "parcellation": Path(a.parcellation).name, "id": a.participant_id,
}}))
sys.exit(1)
""",
    )
    execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    info = json.loads(seen.read_text(encoding="utf-8"))
    assert info["rows"] == [["id"], ["case"]]
    assert info["lesion_exists"] and info["id"] == "case"
    assert (info["fc"], info["subjects"]) == ("fc_row_subject_col.npy", "normative_subjects.csv")
    assert info["parcellation"] == "synthetic_blocks27_2mm.nii.gz"  # from the bundle metadata


def test_engine_flags_that_would_match_by_age_and_sex_are_never_passed():
    source = (STEP_DIR / "src" / "flnm_fast_step" / "step.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))
    assert '"--match-age-sex"' not in code and '"--control-count"' not in code


# --- the real engine --------------------------------------------------------------------------


@engine
def test_reference_run_equals_the_independent_zmap(reference, tmp_path, use_engine):
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    assert result == EXIT_OK
    actual = zmap(out / MAP)
    expected = zmap(reference / "expected" / MAP)
    assert actual.dtype == np.float32 and actual.shape == tuple(SHAPE)
    assert np.allclose(actual, expected, rtol=1e-5, atol=1e-5, equal_nan=True)
    assert np.isfinite(actual).all() and (actual != 0).sum() > 0
    assert np.count_nonzero(actual[make_reference.parcellation(SHAPE) == 0]) == 0
    assert_contract_valid(out)
    prov = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    assert prov.get("warnings", []) == []
    assert prov["parameters"] == {}
    assert [b["id"] for b in prov["bundles"]] == ["synthetic-normative-fc"]
    assert {s["name"] for s in prov["software"]} >= {"flnm-fast engine", "numpy", "nibabel"}
    assert sorted(p.name for p in out.iterdir()) == [MAP, "provenance.json"]


@engine
def test_every_participant_of_the_cohort_is_used(reference, tmp_path, use_engine):
    """No matching and no subset: the expected map over all participants, and not the one over
    a subset (the engine would give the latter with --match-age-sex --control-count)."""
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    assert result == EXIT_OK
    subset = make_reference.expected_zmap(
        make_reference.parcellation(SHAPE),
        zmap(reference / "lesion_mask.nii.gz"),
        make_reference.fc_array(),
        n_subjects=make_reference.N_SUBJECTS // 2,
    )
    assert not np.allclose(zmap(out / MAP), subset, atol=1e-3)


@engine
def test_extra_columns_and_another_parcellation_name_are_accepted(reference, tmp_path, use_engine):
    """A real table also has source_file, site and FD columns (and maybe others): ignored. The
    parcellation is found through the metadata whatever its name."""
    bundle = _bundle_copy(reference, tmp_path, "real-like")
    _rewrite_table(
        bundle,
        lambda rows: [rows[0] + ["source_file", "site_a", "FD"]]
        + [r + [f"f{r[0]}.nii", "0", "0.1"] for r in rows[1:]],
    )
    (bundle / "standard" / "synthetic_blocks27_2mm.nii.gz").rename(
        bundle / "standard" / "firsT_2566.nii.gz"
    )
    _set_metadata(bundle, parcellation="standard/firsT_2566.nii.gz")
    (tmp_path / "run").mkdir()
    result, out = execute(tmp_path / "run", reference / "lesion_mask.nii.gz", bundle, PARAMS)
    assert result == EXIT_OK
    expected = zmap(reference / "expected" / MAP)
    assert np.allclose(zmap(out / MAP), expected, rtol=1e-5, atol=1e-5, equal_nan=True)


@engine
def test_run_is_deterministic(reference, tmp_path, use_engine):
    outputs = []
    for sub in ("a", "b"):
        (tmp_path / sub).mkdir()
        result, out = execute(
            tmp_path / sub, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS
        )
        assert result == EXIT_OK
        outputs.append((out / MAP).read_bytes())
    assert outputs[0] == outputs[1]


@engine
def test_undefined_parcel_is_reported_as_a_warning(reference, tmp_path, use_engine):
    bundle = tmp_path / "nan-bundle"
    make_reference.write_bundle(bundle, SHAPE, AFFINE, undefined_target=26)
    (tmp_path / "run").mkdir()
    result, out = execute(tmp_path / "run", reference / "lesion_mask.nii.gz", bundle, PARAMS)
    assert result == EXIT_OK
    labels = make_reference.parcellation(SHAPE)
    actual = zmap(out / MAP)
    assert np.isnan(actual[labels == 27]).all() and np.isfinite(actual[labels < 27]).all()
    prov = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    assert [w["code"] for w in prov["warnings"]] == ["undefined_parcels"]
    assert_contract_valid(out)


@engine
def test_empty_mask_is_rejected(reference, tmp_path, use_engine):
    empty = tmp_path / "empty.nii.gz"
    make_reference.write_nifti(empty, np.zeros(SHAPE, np.uint8), AFFINE)
    (tmp_path / "run").mkdir()
    result, out = execute(tmp_path / "run", empty, reference / "bundle", PARAMS)
    assert result == EXIT_REJECTED
    assert error_code(out) == "mask_empty"
    assert_contract_valid(out, "error.json")


@engine
def test_lesion_outside_the_parcellation_is_rejected(reference, tmp_path, use_engine):
    data = np.zeros(SHAPE, np.uint8)
    data[1:4, 1:4, 1:4] = 1  # corner of the grid, outside the parcellation box
    outside = tmp_path / "outside.nii.gz"
    make_reference.write_nifti(outside, data, AFFINE)
    (tmp_path / "run").mkdir()
    result, out = execute(tmp_path / "run", outside, reference / "bundle", PARAMS)
    assert result == EXIT_REJECTED
    assert error_code(out) == "lesion_outside_parcellation"
    assert not (out / MAP).exists()
    assert_contract_valid(out, "error.json")


@engine
def test_lesion_in_a_single_parcel_is_processed(reference, tmp_path, use_engine):
    """Engine 1.0 dropped the only parcel and wrote an all-NaN map; 2.x weights it (weight 1)."""
    data = np.zeros(SHAPE, np.uint8)
    data[20, 20, 20] = 1  # one voxel of parcel 1
    single = tmp_path / "single.nii.gz"
    make_reference.write_nifti(single, data, AFFINE)
    (tmp_path / "run").mkdir()
    result, out = execute(tmp_path / "run", single, reference / "bundle", PARAMS)
    assert result == EXIT_OK
    expected = make_reference.expected_zmap(
        make_reference.parcellation(SHAPE),
        data,
        make_reference.fc_array(),
    )
    actual = zmap(out / MAP)
    assert np.isfinite(actual).all() and (actual != 0).any()
    assert np.allclose(actual, expected, rtol=1e-5, atol=1e-5)
    assert_contract_valid(out)


@engine
def test_lowest_overlapped_parcel_is_part_of_the_weighting(
    reference, tmp_path, use_engine
):
    """Regression guard of the engine 1.0 defect (it dropped the lowest overlapped parcel)."""
    labels = make_reference.parcellation(SHAPE)
    mask = zmap(reference / "lesion_mask.nii.gz")
    lowest = int(labels[mask > 0][labels[mask > 0] > 0].min())
    rest = mask.copy()
    rest[labels == lowest] = 0  # the same lesion without the lowest overlapped parcel
    assert 0 < rest.sum() < mask.sum()
    args = (labels, make_reference.fc_array())
    with_lowest = make_reference.expected_zmap(args[0], mask, args[1])
    without_lowest = make_reference.expected_zmap(args[0], rest, args[1])
    assert not np.allclose(with_lowest, without_lowest, atol=1e-3)
    result, out = execute(
        tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS
    )
    assert result == EXIT_OK
    assert np.allclose(zmap(out / MAP), with_lowest, rtol=1e-5, atol=1e-5)
    assert not np.allclose(zmap(out / MAP), without_lowest, atol=1e-3)


@engine
def test_engine_script_is_the_pinned_version():
    import subprocess

    proc = subprocess.run(
        [sys.executable, str(ENGINE), "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc.stdout.strip().endswith(ENGINE_VERSION)


@engine
def test_normative_participant_identifiers_stay_out_of_the_output(
    reference, tmp_path, use_engine
):
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    assert result == EXIT_OK
    published = b"".join(p.read_bytes() for p in out.rglob("*") if p.is_file())
    assert b"synthetic-0" not in published


def test_engine_csv_helpers(tmp_path):
    path = tmp_path / "roi.csv"
    with path.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["roi_label", "roi_index", "z"])
        w.writerow([1, 0, 1.5])
        w.writerow([2, 1, "nan"])
    assert step_module._undefined_parcels(path) == 1
    assert step_module._count_rows(path) == 2
