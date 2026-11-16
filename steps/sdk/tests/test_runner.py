"""Lifecycle tests: success, rejection, internal failure, files in /out."""

from __future__ import annotations

import copy
import hashlib
import json

import nibabel as nib
import numpy as np
import pytest
from sdk_helpers import MANIFEST, write_qc
from stroke_step_sdk import (
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_REJECTED,
    InputRejected,
    QcFailed,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 10


def test_success_writes_valid_provenance(run):
    def fn(ctx):
        assert ctx.params == {"threshold": 1.0, "seed": 7}  # defaults applied
        assert ctx.has_input("lesion_mask") and not ctx.has_input("ct")
        ctx.warn("small_lesion", "The lesion is small.", port="lesion_mask")
        ctx.add_software("tool-x", "2.0")
        write_qc(ctx)

    assert run.run(fn) == EXIT_OK
    assert run.files() == {"lesion_qc.json", "provenance.json"}
    prov = run.json("provenance.json")
    assert prov["status"] == "succeeded"
    assert prov["step"] == {"name": "sdk-test-step", "version": "1.0.0"}
    assert [r["port"] for r in prov["inputs"]] == ["lesion_mask"]
    assert [r["port"] for r in prov["outputs"]] == ["lesion_qc"]
    assert prov["parameters"] == {"threshold": 1.0, "seed": 7}
    assert prov["warnings"] == [
        {
            "code": "small_lesion",
            "message": "The lesion is small.",
            "port": "lesion_mask",
        }
    ]
    assert {"compneuro-step-sdk", "python", "tool-x"} <= {
        s["name"] for s in prov["software"]
    }
    assert prov["started_at"] <= prov["finished_at"]
    assert run.validate() == []


def test_hashes_match_files(run):
    run.run(write_qc)
    prov = run.json("provenance.json")
    out = (run.out_dir / "lesion_qc.json").read_bytes()
    assert prov["outputs"][0]["sha256"] == hashlib.sha256(out).hexdigest()
    assert prov["outputs"][0]["size_bytes"] == len(out)
    inp = (run.in_dir / "lesion_mask.nii.gz").read_bytes()
    assert prov["inputs"][0]["sha256"] == hashlib.sha256(inp).hexdigest()


def test_partial_params_get_defaults_and_overrides(run):
    run.write_params({"threshold": 2.5})
    seen = {}

    def fn(ctx):
        seen.update(ctx.params)
        write_qc(ctx)

    assert run.run(fn) == EXIT_OK
    assert seen == {"threshold": 2.5, "seed": 7}


def test_optional_input_and_optional_output(run):
    nib.save(
        nib.Nifti1Image(np.zeros((4, 4, 4), np.float32), np.eye(4)),
        str(run.in_dir / "ct.nii.gz"),
    )

    def fn(ctx):
        assert ctx.has_input("ct")
        write_qc(ctx)
        ctx.output_path("qc_figure").write_bytes(PNG)

    assert run.run(fn) == EXIT_OK
    prov = run.json("provenance.json")
    assert [r["port"] for r in prov["inputs"]] == ["lesion_mask", "ct"]
    assert [r["port"] for r in prov["outputs"]] == ["lesion_qc", "qc_figure"]
    assert run.validate() == []


def test_extra_directory_is_allowed(run):
    def fn(ctx):
        write_qc(ctx)
        ctx.extra_path("debug.txt").write_text("hello", encoding="utf-8")

    assert run.run(fn) == EXIT_OK
    assert (run.out_dir / "extra" / "debug.txt").exists()


def test_input_rejected_exit_10(run):
    def fn(ctx):
        write_qc(ctx)  # partial output must be removed on rejection
        raise InputRejected(
            "mask_empty",
            "The lesion mask has no voxels set.",
            port="lesion_mask",
            details={"voxels": 0},
        )

    assert run.run(fn) == EXIT_REJECTED
    assert run.files() == {"error.json", "provenance.json"}
    err = run.json("error.json")
    assert err["category"] == "input_rejected" and err["code"] == "mask_empty"
    assert err["port"] == "lesion_mask" and err["details"] == {"voxels": 0}
    prov = run.json("provenance.json")
    assert prov["status"] == "rejected" and prov["outputs"] == []
    assert run.validate() == []


def test_qc_failed_exit_10(run):
    def fn(ctx):
        raise QcFailed("low_quality", "The quality check failed.")

    assert run.run(fn) == EXIT_REJECTED
    assert run.json("error.json")["category"] == "qc_failed"
    assert run.validate() == []


def test_undeclared_error_code_is_internal_failure(run):
    def fn(ctx):
        raise InputRejected("not_declared", "Nope.")

    assert run.run(fn) == EXIT_INTERNAL
    err = run.json("error.json")
    assert err["category"] == "internal" and err["code"] == "undeclared_error_code"
    assert run.json("provenance.json")["status"] == "failed"
    assert run.validate() == []


def test_category_must_match_declaration(run):
    def fn(ctx):
        raise QcFailed("mask_empty", "Declared as input_rejected.")

    assert run.run(fn) == EXIT_INTERNAL
    assert run.json("error.json")["code"] == "error_category_mismatch"


def test_message_with_path_is_refused(run):
    def fn(ctx):
        raise InputRejected("mask_empty", "Cannot read /in/lesion_mask.nii.gz")

    assert run.run(fn) == EXIT_INTERNAL
    assert run.json("error.json")["code"] == "invalid_error_report"


def test_message_too_long_is_refused(run):
    def fn(ctx):
        raise InputRejected("mask_empty", "x" * 501)

    assert run.run(fn) == EXIT_INTERNAL
    assert run.json("error.json")["code"] == "invalid_error_report"


def test_unknown_port_in_error_is_refused(run):
    def fn(ctx):
        raise InputRejected("mask_empty", "Bad port.", port="nonexistent")

    assert run.run(fn) == EXIT_INTERNAL


def test_unexpected_exception_is_internal(run):
    def fn(ctx):
        write_qc(ctx)
        raise RuntimeError("boom /secret/path")

    assert run.run(fn) == EXIT_INTERNAL
    assert run.files() == {"error.json", "provenance.json"}
    err = run.json("error.json")
    assert err["category"] == "internal" and err["code"] == "internal_error"
    assert "secret" not in err["message"]
    assert err["details"] == {"exception": "RuntimeError"}
    assert run.validate() == []


def test_system_exit_in_step_is_internal(run):
    def fn(ctx):
        raise SystemExit(0)

    assert run.run(fn) == EXIT_INTERNAL
    assert run.json("error.json")["code"] == "unexpected_exit"


def test_missing_required_output(run):
    assert run.run(lambda ctx: None) == EXIT_INTERNAL
    assert run.json("error.json")["code"] == "missing_required_output"
    assert run.json("provenance.json")["status"] == "failed"


def test_invalid_output_format(run):
    def fn(ctx):
        ctx.output_path("lesion_qc").write_text("not json", encoding="utf-8")

    assert run.run(fn) == EXIT_INTERNAL
    assert run.json("error.json")["code"] == "invalid_output"


def test_invalid_png_output(run):
    def fn(ctx):
        write_qc(ctx)
        ctx.output_path("qc_figure").write_bytes(b"GIF89a")

    assert run.run(fn) == EXIT_INTERNAL
    assert run.json("error.json")["code"] == "invalid_output"


def test_stray_file_in_out_is_a_failure(run):
    def fn(ctx):
        write_qc(ctx)
        (ctx.out_dir / "notes.txt").write_text("x", encoding="utf-8")

    assert run.run(fn) == EXIT_INTERNAL
    assert run.json("error.json")["code"] == "unexpected_output_file"


def test_undeclared_port_name_cannot_be_used(run):
    def fn(ctx):
        with pytest.raises(KeyError):
            ctx.output_path("nope")
        with pytest.raises(KeyError):
            ctx.input_path("nope")
        with pytest.raises(FileNotFoundError):
            ctx.input_path("ct")  # optional and absent
        with pytest.raises(ValueError):
            ctx.warn("Bad Code", "x")
        with pytest.raises(ValueError):
            ctx.extra_path("../escape")
        write_qc(ctx)

    assert run.run(fn) == EXIT_OK


def test_seed_is_deterministic(run):
    draws = []

    def fn(ctx):
        draws.append(ctx.rng().random())
        write_qc(ctx)

    assert run.run(fn) == EXIT_OK
    assert draws[0] == float(np.random.default_rng(7).random())


def test_invalid_manifest_returns_internal_without_files(run):
    bad = copy.deepcopy(MANIFEST)
    bad["kind"] = "Nope"
    run.manifest_path.write_text(json.dumps(bad), encoding="utf-8")
    assert run.run(lambda ctx: None) == EXIT_INTERNAL
    assert run.files() == set()


def test_manifest_with_newer_minor_contract_version_is_rejected(run):
    bad = copy.deepcopy(MANIFEST)
    bad["contract_version"] = "1.2"  # newer than the contract the SDK implements
    run.manifest_path.write_text(json.dumps(bad), encoding="utf-8")
    assert run.run(lambda ctx: None) == EXIT_INTERNAL
    assert run.files() == set()


def test_two_runs_in_one_process(run):
    assert run.run(write_qc) == EXIT_OK
    run.clear_out()
    assert run.run(write_qc) == EXIT_OK


def test_logs_start_with_level_word(run, capsys):
    def fn(ctx):
        ctx.log.info("hello")
        ctx.warn("small_lesion", "Careful.")
        write_qc(ctx)

    run.run(fn)
    captured = capsys.readouterr()
    lines = [x for x in (captured.out + captured.err).splitlines() if x]
    assert lines
    assert all(
        x.split(" ", 1)[0] in {"DEBUG", "INFO", "WARNING", "ERROR"} for x in lines
    )
    assert any(x.startswith("WARNING small_lesion") for x in captured.err.splitlines())
