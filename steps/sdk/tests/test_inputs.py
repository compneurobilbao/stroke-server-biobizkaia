"""Input validation, parameters, bundles (contract 1.4, 1.5, 2.4 item 5, artefact types 3)."""

from __future__ import annotations

import json

import nibabel as nib
import numpy as np
import pytest
from sdk_helpers import MANIFEST, mni2_grid, write_mask, write_qc
from stroke_step_sdk import EXIT_INTERNAL, EXIT_OK, EXIT_REJECTED, formats


def rejected(run, code, port=None, cross_check=True):
    """The run was rejected with exit 10 and a valid error.json/provenance.json."""
    called = []

    def fn(ctx):  # must not be reached
        called.append(True)

    assert run.run(fn) == EXIT_REJECTED
    assert not called, "run() must not start when an input is rejected"
    err = run.json("error.json")
    assert err["category"] == "input_rejected" and err["code"] == code
    assert err.get("port") == port
    assert run.files() == {"error.json", "provenance.json"}
    assert run.json("provenance.json")["status"] == "rejected"
    if cross_check:
        assert run.validate() == []
    return err


def test_missing_required_input(run):
    (run.in_dir / "lesion_mask.nii.gz").unlink()
    rejected(run, "input_missing", "lesion_mask")


def test_mask_not_binary(run):
    shape, _ = mni2_grid()
    values = np.zeros(shape, np.float32)
    values[40:44, 50:54, 40:44] = np.arange(64, dtype=np.float32).reshape(4, 4, 4)
    write_mask(run.in_dir / "lesion_mask.nii.gz", values)
    err = rejected(run, "input_format_invalid", "lesion_mask")
    assert err["details"]["distinct_values"] == 64
    assert err["details"]["type"] == "lesion_mask_mni"


def test_mask_with_nan_is_not_binary(run):
    shape, _ = mni2_grid()
    values = np.zeros(shape, np.float32)
    values[0, 0, 0] = np.nan
    write_mask(run.in_dir / "lesion_mask.nii.gz", values)
    rejected(run, "input_format_invalid", "lesion_mask")


def test_mask_on_wrong_grid(run):
    write_mask(
        run.in_dir / "lesion_mask.nii.gz", np.zeros((10, 10, 10), np.uint8), np.eye(4)
    )
    rejected(run, "input_format_invalid", "lesion_mask")


def test_mask_with_shifted_affine(run):
    _, affine = mni2_grid()
    shifted = affine.copy()
    shifted[0, 3] += 0.01  # more than the 1e-4 mm tolerance
    write_mask(run.in_dir / "lesion_mask.nii.gz", affine=shifted)
    rejected(run, "input_format_invalid", "lesion_mask")


def test_mask_within_affine_tolerance_is_accepted(run):
    _, affine = mni2_grid()
    nudged = affine.copy()
    nudged[0, 3] += 5e-5
    write_mask(run.in_dir / "lesion_mask.nii.gz", affine=nudged)
    assert run.run(write_qc) == EXIT_OK


def test_input_not_gzip(run):
    (run.in_dir / "lesion_mask.nii.gz").write_bytes(b"not a nifti")
    rejected(run, "input_format_invalid", "lesion_mask")


def test_input_corrupt_gzip(run):
    import gzip

    (run.in_dir / "lesion_mask.nii.gz").write_bytes(gzip.compress(b"garbage" * 100))
    rejected(run, "input_format_invalid", "lesion_mask")


def test_unexpected_files_in_input_are_ignored(run):
    (run.in_dir / "other.txt").write_text("x", encoding="utf-8")
    assert run.run(write_qc) == EXIT_OK


def test_unreadable_params_file_is_internal(run):
    (run.in_dir / "params.json").write_text("{not json", encoding="utf-8")
    assert run.run(write_qc) == EXIT_INTERNAL
    assert run.json("error.json")["code"] == "params_unreadable"


def test_missing_params_file_is_internal(run):
    (run.in_dir / "params.json").unlink()
    assert run.run(write_qc) == EXIT_INTERNAL


def test_params_not_an_object_is_internal(run):
    (run.in_dir / "params.json").write_text("[1]", encoding="utf-8")
    assert run.run(write_qc) == EXIT_INTERNAL


@pytest.mark.parametrize(
    "params",
    [{"threshold": -1}, {"threshold": "high"}, {"unknown": 1}, {"seed": 1.5}],
)
def test_invalid_parameters_are_rejected(run, params):
    run.write_params(params)
    # validate.py cross-checks provenance.parameters against the manifest schema, which
    # cannot hold for rejected parameters (a contract gap; see the PR), so skip that part.
    rejected(run, "invalid_parameters", cross_check=False)


def test_parameters_declared_without_standard_codes_still_run(make_run):
    import copy

    manifest = copy.deepcopy(MANIFEST)
    manifest["errors"] = [e for e in manifest["errors"] if e["code"] == "mask_empty"]
    run = make_run(manifest)
    assert run.run(write_qc) == EXIT_OK  # only a startup warning
    (run.in_dir / "lesion_mask.nii.gz").unlink()
    run.clear_out()
    assert run.run(write_qc) == EXIT_INTERNAL  # input_missing is not declared
    assert run.json("error.json")["code"] == "undeclared_error_code"


def test_bundle_is_recorded(run, ref_dir):
    seen = {}

    def fn(ctx):
        seen["path"] = ctx.bundle_path("atlas")
        write_qc(ctx)

    assert run.run(fn, ref_dir=ref_dir) == EXIT_OK
    assert seen["path"] == ref_dir / "atlas"
    prov = run.json("provenance.json")
    assert prov["bundles"] == [
        {
            "name": "atlas",
            "id": "toy-normative-connectome",
            "version": "1.0.0",
            "content_digest": "sha256:dd625c79ae75f2bf5f6f94aa3edf4f6fb4d874a7e65c0c92c6e4d019cc9d3a70",
        }
    ]
    assert run.validate() == []


def test_optional_bundle_absent(run):
    def fn(ctx):
        with pytest.raises(KeyError):
            ctx.bundle_path("atlas")
        write_qc(ctx)

    assert run.run(fn) == EXIT_OK
    assert run.json("provenance.json")["bundles"] == []


def _bundle_manifest(**requirement):
    import copy

    manifest = copy.deepcopy(MANIFEST)
    manifest["bundles"] = [
        {"name": "atlas", "type": "normative-connectome", **requirement}
    ]
    return manifest


def test_required_bundle_missing_is_internal(make_run):
    run = make_run(_bundle_manifest(required=True))
    assert run.run(write_qc) == EXIT_INTERNAL
    assert run.json("error.json")["code"] == "bundle_missing"


def test_bundle_type_mismatch_is_internal(make_run, ref_dir):
    manifest = _bundle_manifest()
    manifest["bundles"][0]["type"] = "tract-atlas"
    run = make_run(manifest)
    assert run.run(write_qc, ref_dir=ref_dir) == EXIT_INTERNAL
    assert run.json("error.json")["code"] == "bundle_mismatch"


def test_bundle_version_range_is_enforced(make_run, ref_dir):
    run = make_run(_bundle_manifest(version="^2.0.0"))
    assert run.run(write_qc, ref_dir=ref_dir) == EXIT_INTERNAL
    assert run.json("error.json")["code"] == "bundle_mismatch"
    run.clear_out()
    run.manifest_path.write_text(
        run.manifest_path.read_text(encoding="utf-8").replace("^2.0.0", "^1.0.0"),
        encoding="utf-8",
    )
    assert run.run(write_qc, ref_dir=ref_dir) == EXIT_OK


# --- format checks, direct --------------------------------------------------------------


def test_check_file_json_with_schema(tmp_path):
    good = tmp_path / "g.json"
    good.write_text(
        ('{"contract_version": "1.0", "kind": "RunProvenance"}'),
        encoding="utf-8",
    )
    # run_provenance is validated against the provenance schema: this one is incomplete
    assert formats.check_file(good, "run_provenance")
    bad = tmp_path / "b.json"
    bad.write_bytes(b"\xff\xfe")
    assert formats.check_file(bad, "report_json")


def test_check_file_valid_run_provenance(tmp_path):
    from stroke_step_sdk import contracts

    path = contracts.contracts_dir() / "examples" / "provenance-run.json"
    assert formats.check_file(path, "run_provenance") == []


def test_check_file_tsv(tmp_path):
    ok = tmp_path / "a.tsv"
    ok.write_text("tract\tdisconnection\textra\nA\t0.1\tx\n", encoding="utf-8")
    assert formats.check_file(ok, "tract_disconnection") == []
    bad = tmp_path / "b.tsv"
    bad.write_text("tract\tvalue\nA\t0.1\n", encoding="utf-8")
    problems = formats.check_file(bad, "tract_disconnection")
    assert problems and "disconnection" in problems[0].message
    empty = tmp_path / "c.tsv"
    empty.write_text("", encoding="utf-8")
    assert formats.check_file(empty, "tract_disconnection")


def test_check_file_signatures(tmp_path):
    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.7\n")
    assert formats.check_file(pdf, "report_pdf") == []
    pdf.write_bytes(b"nope")
    assert formats.check_file(pdf, "report_pdf")
    png = tmp_path / "f.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert formats.check_file(png, "qc_figure") == []


def test_check_file_nifti_grid_res1_and_float(tmp_path):
    from stroke_step_sdk import contracts

    grid = contracts.registry()["grids"]["MNI152NLin6Asym-res1"]
    img = nib.Nifti1Image(
        np.zeros(grid["shape"], np.float32), np.array(grid["affine"], float)
    )
    path = tmp_path / "ct.nii.gz"
    nib.save(img, str(path))
    assert formats.check_file(path, "ct_mni") == []
    assert formats.check_file(path, "lesion_mask_mni")  # wrong grid for 2 mm type
    # a 4-D image is not on the 3-D grid
    four = nib.Nifti1Image(np.zeros((*grid["shape"][:2], 2, 1), np.uint8), np.eye(4))
    nib.save(four, str(tmp_path / "x.nii.gz"))
    assert formats.check_file(tmp_path / "x.nii.gz", "ct_mni")


def test_params_json_roundtrip_is_unchanged(run):
    run.write_params({"threshold": 3, "seed": 11})
    run.run(write_qc)
    assert run.json("provenance.json")["parameters"] == {"threshold": 3, "seed": 11}
    assert json.loads((run.in_dir / "params.json").read_text())["seed"] == 11
