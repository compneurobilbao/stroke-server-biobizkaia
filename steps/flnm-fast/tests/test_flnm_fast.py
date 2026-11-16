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


PARAMS = {"sex": "female", "age_band": "60-64", "control_count": 16}


# --- manifest, bundle and reference data (no engine needed) ---------------------------------


def test_manifest_is_valid_contract_1_1_real_step():
    issues = contracts.validator().validate_files([MANIFEST])
    assert [i.message for i in issues if i.severity == "error"] == []
    doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert doc["contract_version"] == "1.1"
    assert doc["provides"] == "functional-disconnection"
    assert doc.get("stub", False) is False
    assert doc["outputs"][0]["type"] == "fdisconnection_map"
    assert doc["reference_test"]["generator"] == "tests/make_reference.py"


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


def test_reference_cohort_exercises_the_tie_break():
    """The 16th and 17th nearest females are tied in age distance and in age: the index decides."""
    rows = make_reference.subjects()
    females = sorted(
        (abs(r["age"] - 62.0), r["age"], r["subject_index"]) for r in rows if r["sex"] == "F"
    )
    n = make_reference.REFERENCE_CONTROLS
    assert females[n - 1][:2] == females[n][:2]
    chosen = make_reference.select_controls(rows, 62.0, "F", n)
    assert females[n - 1][2] in chosen and females[n][2] not in chosen
    assert chosen == sorted(chosen) and len(chosen) == n


def test_independent_statistics_match_scipy():
    stats = pytest.importorskip("scipy.stats")
    for df in (2, 19, 59):
        for t in (-12.0, -2.5, -0.1, 0.0, 0.3, 4.0, 9.5):
            p = float(2.0 * stats.t.sf(abs(t), df))
            assert make_reference.two_sided_p(t, df) == pytest.approx(p, rel=1e-9, abs=1e-300)
            z = float(np.sign(t) * stats.norm.isf(max(p, np.finfo(float).tiny) / 2.0))
            assert make_reference.signed_z(t, df) == pytest.approx(z, rel=1e-9, abs=1e-12)


def test_age_from_band():
    assert step_module.age_from_band("65-69") == 67.0
    assert step_module.age_from_band("90-104") == 97.0
    from stroke_step_sdk import InputRejected

    with pytest.raises(InputRejected):
        step_module.age_from_band("70-65")


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        (["F", "M"], ("F", "M")),
        (["female", "male"], ("FEMALE", "MALE")),
        (["f", "m", "F", "M"], ("F", "M")),
    ],
)
def test_sex_label_follows_the_normative_table(tmp_path, labels, expected):
    table = tmp_path / "normative_subjects.csv"
    rows = ["subject_index,eid,age,sex"] + [f"{i},x{i},50,{s}" for i, s in enumerate(labels)]
    table.write_text("\n".join(rows) + "\n", encoding="utf-8")
    assert step_module.normative_sex_label(table, "female") == expected[0]
    assert step_module.normative_sex_label(table, "male") == expected[1]


def test_sex_label_refuses_unrecognisable_tables(tmp_path):
    from stroke_step_sdk import InternalFailure

    table = tmp_path / "normative_subjects.csv"
    table.write_text("subject_index,eid,age,sex\n0,x,50,1\n1,y,50,2\n", encoding="utf-8")
    with pytest.raises(InternalFailure) as info:
        step_module.normative_sex_label(table, "female")
    assert info.value.code == "bundle_layout_unsupported"


# --- rejections that do not need the engine --------------------------------------------------


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"age_band": "60-64"}, "participant_sex_missing"),
        ({"sex": "female"}, "participant_age_missing"),
        ({}, "participant_sex_missing"),
    ],
)
def test_missing_case_metadata_is_rejected(reference, tmp_path, params, code):
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", params)
    assert result == EXIT_REJECTED
    assert error_code(out) == code
    assert not (out / MAP).exists()
    assert_contract_valid(out, "error.json")


@pytest.mark.parametrize(
    "params",
    [
        {"sex": "other"},
        {"sex": "female", "age_band": "sixty"},
        {"sex": "female", "age_band": "60-64", "control_count": 1},
        {"sex": "female", "age_band": "60-64", "unknown": 1},
    ],
)
def test_invalid_parameters_use_the_reserved_code(reference, tmp_path, params):
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
                         "normative-subjects", "fc", "out-dir", "control-count"):
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
        ("Only 5 normative subjects with sex=F; need 20", "insufficient_controls", "input_rejected"),
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


def test_empty_overlap_table_is_a_single_parcel_rejection(reference, tmp_path, monkeypatch):
    """The engine succeeds with an all-NaN map when no parcel is left to weight."""
    fake_engine(
        tmp_path,
        monkeypatch,
        r"""
(out / "case_zmap.nii.gz").write_bytes(b"x")
(out / "case_overlap.csv").write_text("roi_label,roi_index,overlap_voxels,weight\n")
(out / "case_controls.csv").write_text("subject_index,eid,age,sex\n")
(out / "case_roi_z.csv").write_text("roi_label,roi_index,z\n1,0,nan\n")
""",
    )
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    assert result == EXIT_REJECTED
    assert error_code(out) == "lesion_single_parcel"
    assert not (out / MAP).exists()
    assert_contract_valid(out, "error.json")


def test_unknown_engine_failure_is_internal_and_leaks_no_path(reference, tmp_path, monkeypatch):
    fake_engine(tmp_path, monkeypatch, FAIL.format(text=f"boom in {tmp_path}/x"))
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    assert result == EXIT_INTERNAL
    error = json.loads((out / "error.json").read_text(encoding="utf-8"))
    assert error["code"] == "engine_failed"
    assert str(tmp_path) not in json.dumps(error)


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
    seen = tmp_path / "seen.json"
    fake_engine(
        tmp_path,
        monkeypatch,
        f"""
import json
rows = list(csv.reader(open(a.participants), delimiter="\\t"))
lesion = Path(a.lesion_dir) / (a.participant_id + ".nii.gz")
Path({str(seen)!r}).write_text(json.dumps({{
    "rows": rows, "lesion_exists": lesion.is_file(), "control_count": a.control_count,
    "fc": Path(a.fc).name, "subjects": Path(a.normative_subjects).name,
    "parcellation": Path(a.parcellation).name, "id": a.participant_id,
}}))
sys.exit(1)
""",
    )
    execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    info = json.loads(seen.read_text(encoding="utf-8"))
    assert info["rows"] == [["id", "age", "sex"], ["case", "62.0", "F"]]
    assert info["lesion_exists"] and info["control_count"] == "16" and info["id"] == "case"
    assert (info["fc"], info["subjects"]) == (
        "fc_row_subject_col.npy",
        "normative_subjects.csv",
    )
    assert info["parcellation"] == "schaefer1000_tian54_2mm.nii.gz"


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
    assert [w["code"] for w in prov["warnings"]] == ["lowest_overlapped_parcel_omitted"]
    assert [b["id"] for b in prov["bundles"]] == ["synthetic-normative-fc"]
    assert {s["name"] for s in prov["software"]} >= {"flnm-fast engine", "numpy", "nibabel"}
    assert sorted(p.name for p in out.iterdir()) == [MAP, "provenance.json"]


@engine
@pytest.mark.parametrize(
    ("sex", "band", "age", "count"),
    [
        ("female", "60-64", 62.0, 8),  # inside the group of age 62: the subject index decides
        ("female", "60-64", 62.0, 40),  # reaches ages 61 and 63, tied in distance: age decides
        ("male", "45-49", 47.0, 10),
        ("male", "45-49", 47.0, 60),
        ("female", "90-104", 97.0, 1000),  # the engine default; ages far from the cohort's
    ],
)
def test_control_selection_and_ties_follow_the_independent_computation(
    reference, tmp_path, use_engine, sex, band, age, count
):
    params = {"sex": sex, "age_band": band, "control_count": count}
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", params)
    assert result == EXIT_OK
    expected = make_reference.expected_zmap(
        make_reference.parcellation(SHAPE),
        zmap(reference / "lesion_mask.nii.gz"),
        make_reference.fc_array(),
        make_reference.subjects(),
        age=age,
        sex=sex[0].upper(),
        count=count,
    )
    assert np.allclose(zmap(out / MAP), expected, rtol=1e-5, atol=1e-5)
    assert not np.allclose(zmap(out / MAP), zmap(reference / "expected" / MAP), atol=1e-3)


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
    make_reference.write_bundle(bundle, SHAPE, AFFINE, constant_target=26)
    (tmp_path / "run").mkdir()
    result, out = execute(tmp_path / "run", reference / "lesion_mask.nii.gz", bundle, PARAMS)
    assert result == EXIT_OK
    labels = make_reference.parcellation(SHAPE)
    actual = zmap(out / MAP)
    assert np.isnan(actual[labels == 27]).all() and np.isfinite(actual[labels < 27]).all()
    prov = json.loads((out / "provenance.json").read_text(encoding="utf-8"))
    assert [w["code"] for w in prov["warnings"]] == [
        "lowest_overlapped_parcel_omitted",
        "undefined_parcels",
    ]
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
def test_lesion_in_a_single_parcel_is_rejected(reference, tmp_path, use_engine):
    data = np.zeros(SHAPE, np.uint8)
    data[20, 20, 20] = 1  # one voxel of parcel 1
    single = tmp_path / "single.nii.gz"
    make_reference.write_nifti(single, data, AFFINE)
    (tmp_path / "run").mkdir()
    result, out = execute(tmp_path / "run", single, reference / "bundle", PARAMS)
    assert result == EXIT_REJECTED
    assert error_code(out) == "lesion_single_parcel"
    assert_contract_valid(out, "error.json")


@engine
def test_engine_omits_the_lowest_overlapped_parcel(reference, tmp_path, use_engine):
    """Pins the known issue (README.md): fails once the base image no longer does this."""
    assert step_module.ENGINE_DROPS_LOWEST_OVERLAPPED_PARCEL
    assert make_reference.ENGINE_DROPS_LOWEST_OVERLAPPED_PARCEL
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", PARAMS)
    assert result == EXIT_OK
    faithful = make_reference.expected_zmap(
        make_reference.parcellation(SHAPE),
        zmap(reference / "lesion_mask.nii.gz"),
        make_reference.fc_array(),
        make_reference.subjects(),
        age=62.0,
        sex="F",
        count=16,
        drop_lowest=False,
    )
    assert not np.allclose(zmap(out / MAP), faithful, atol=1e-3)


@engine
def test_too_few_controls_are_rejected(reference, tmp_path, use_engine):
    params = dict(PARAMS, control_count=5000)
    result, out = execute(tmp_path, reference / "lesion_mask.nii.gz", reference / "bundle", params)
    assert result == EXIT_REJECTED
    assert error_code(out) == "insufficient_controls"
    assert_contract_valid(out, "error.json")


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
