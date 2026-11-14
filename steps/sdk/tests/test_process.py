"""Process-level behaviour: command line, environment, SIGTERM, reference-test helpers."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest
from sdk_helpers import write_qc
from stroke_step_sdk import EXIT_OK, EXIT_TERMINATED, _env, cli, reftest
from stroke_step_sdk.manifest import load_manifest

SCRIPT = textwrap.dedent(
    """
    import sys, time
    from stroke_step_sdk import main

    def run(ctx):
        print("INFO working", flush=True)
        if "--sleep" in sys.argv:
            time.sleep(60)
        write(ctx)

    def write(ctx):
        ctx.output_path("lesion_qc").write_text('{"volume_ml": 1}', encoding="utf-8")

    argv = [a for a in sys.argv[1:] if a != "--sleep"]
    sys.exit(main(run, argv))
    """
)


def _command(run, tmp_path, *extra):
    script = tmp_path / "run_script.py"
    script.write_text(SCRIPT, encoding="utf-8")
    return [
        sys.executable,
        str(script),
        "--in",
        str(run.in_dir),
        "--out",
        str(run.out_dir),
        "--params",
        str(run.in_dir / "params.json"),
        "--manifest",
        str(run.manifest_path),
        *extra,
    ]


def test_command_line_end_to_end(run, tmp_path):
    proc = subprocess.run(
        _command(run, tmp_path),
        capture_output=True,
        text=True,
        env={**os.environ, **run.env},
    )
    assert proc.returncode == EXIT_OK, proc.stderr
    assert proc.stdout.startswith("INFO starting step sdk-test-step 1.0.0")
    assert run.validate() == []


def test_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(lambda ctx: None, ["--help"])
    assert exc.value.code == 0
    assert "--in" in capsys.readouterr().out


def test_defaults_are_the_fixed_paths():
    args = cli.build_parser().parse_args([])
    assert (args.in_dir, args.out_dir, args.params) == ("/in", "/out", None)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")
def test_sigterm_stops_promptly_and_reports(run, tmp_path):
    proc = subprocess.Popen(
        _command(run, tmp_path, "--sleep"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **run.env},
    )
    for line in proc.stdout:  # wait until run() is executing
        if "working" in line:
            break
    proc.send_signal(signal.SIGTERM)
    start = time.time()
    code = proc.wait(timeout=20)
    assert time.time() - start < 10
    assert code == EXIT_TERMINATED
    assert run.files() == {"error.json", "provenance.json"}
    err = run.json("error.json")
    assert err["code"] == "terminated" and err["category"] == "internal"
    prov = run.json("provenance.json")
    assert prov["status"] == "failed" and prov["outputs"] == []
    assert run.validate() == []  # contract 1.1, step-contract 2.4 item 6 and 4.4


def test_thread_variables_follow_step_cpus():
    env = {"STEP_CPUS": "3", "TMPDIR": "/scratch"}
    _env.apply(env)
    for name in _env.THREAD_VARIABLES:
        assert env[name] == "3"
    assert env["MPLCONFIGDIR"].startswith("/scratch")
    assert _env.cpus(env) == 3


def test_executor_provided_thread_variables_are_kept():
    env = {"STEP_CPUS": "2", "OMP_NUM_THREADS": "2"}
    _env.apply(env)
    assert env["OMP_NUM_THREADS"] == "2"


def test_bad_step_cpus_falls_back_to_one():
    assert _env.cpus({"STEP_CPUS": "x"}) == 1
    assert _env.cpus({}) == 1
    assert _env.cpus({"STEP_CPUS": "0"}) == 1


def test_context_reports_granted_resources(run):
    run.env.update(STEP_CPUS="2", STEP_MEMORY_MB="512", STEP_GPUS="0")
    seen = {}

    def fn(ctx):
        seen.update(cpus=ctx.cpus, memory=ctx.memory_mb, gpus=ctx.gpus)
        write_qc(ctx)

    assert run.run(fn) == EXIT_OK
    assert seen == {"cpus": 2, "memory": 512, "gpus": 0}


def test_input_directory_is_not_modified(run):
    before = sorted((p.name, p.stat().st_size) for p in run.in_dir.iterdir())
    run.run(write_qc)
    assert before == sorted((p.name, p.stat().st_size) for p in run.in_dir.iterdir())


# --- reference-test helpers -----------------------------------------------------------


def test_compare_json_tolerances():
    assert (
        reftest.compare_json(
            {"a": 1.0, "b": [1, 2]}, {"a": 1.0005, "b": [1, 2]}, 0, 1e-3
        )
        is None
    )
    assert reftest.compare_json({"a": 1.0}, {"a": 1.1}, 0, 1e-3)
    assert reftest.compare_json({"a": 1}, {"b": 1}, 0, 0)
    assert reftest.compare_json([1], [1, 2], 0, 0)
    assert reftest.compare_json({"a": True}, {"a": 1}, 0, 0)
    assert reftest.compare_json("x", "x", 0, 0) is None


def test_compare_dice_and_allclose(tmp_path):
    import nibabel as nib
    import numpy as np

    a = np.zeros((6, 6, 6), np.uint8)
    a[1:4, 1:4, 1:4] = 1
    b = a.copy()
    b[1:4, 1:4, 3] = 0  # remove one slab
    pa, pb = tmp_path / "a.nii.gz", tmp_path / "b.nii.gz"
    nib.save(nib.Nifti1Image(a, np.eye(4)), str(pa))
    nib.save(nib.Nifti1Image(b, np.eye(4)), str(pb))
    assert reftest.compare_dice(pa, pa, 1.0) is None
    assert reftest.compare_dice(pa, pb, 0.79) is None  # dice is 0.8
    assert reftest.compare_dice(pa, pb, 0.85)
    assert reftest.compare_allclose(pa, pa, 0, 0) is None
    assert reftest.compare_allclose(pa, pb, 0, 0.5)
    assert reftest.compare_allclose(pa, pb, 0, 2) is None


def test_compare_allclose_tsv(tmp_path):
    a, b = tmp_path / "a.tsv", tmp_path / "b.tsv"
    a.write_text("tract\tdisconnection\nT1\t0.10\n", encoding="utf-8")
    b.write_text("tract\tdisconnection\nT1\t0.11\n", encoding="utf-8")
    assert reftest.compare_allclose(a, b, 0, 0.02) is None
    assert reftest.compare_allclose(a, b, 0, 0.001)


def test_check_run_directory_detects_violations(run):
    manifest = load_manifest(run.manifest_path)
    assert run.run(write_qc) == EXIT_OK
    assert reftest.check_run_directory(manifest, run.in_dir, run.out_dir, 0) == []
    (run.out_dir / "stray.txt").write_text("x", encoding="utf-8")
    problems = reftest.check_run_directory(manifest, run.in_dir, run.out_dir, 0)
    assert any("stray.txt" in p for p in problems)
    (run.out_dir / "stray.txt").unlink()
    (run.out_dir / "lesion_qc.json").write_text(
        json.dumps({"volume_ml": 2}), encoding="utf-8"
    )
    problems = reftest.check_run_directory(manifest, run.in_dir, run.out_dir, 0)
    assert any("differs from provenance" in p for p in problems)
    (run.out_dir / "provenance.json").unlink()
    assert "provenance.json is missing" in reftest.check_run_directory(
        manifest, run.in_dir, run.out_dir, 0
    )


def test_check_run_directory_forbids_outputs_after_exit_10(run):
    manifest = load_manifest(run.manifest_path)
    (run.in_dir / "lesion_mask.nii.gz").unlink()
    assert run.run(write_qc) == 10
    assert reftest.check_run_directory(manifest, run.in_dir, run.out_dir, 10) == []
    (run.out_dir / "lesion_qc.json").write_text("{}", encoding="utf-8")
    problems = reftest.check_run_directory(manifest, run.in_dir, run.out_dir, 10)
    assert any("lesion_qc.json" in p for p in problems)


def _reftest_step_dir(tmp_path, generator_body):
    import yaml
    from sdk_helpers import MANIFEST

    step_dir = tmp_path / "step"
    (step_dir / "tests").mkdir(parents=True)
    manifest = dict(MANIFEST)
    manifest["reference_test"] = {
        "generator": "tests/make_reference.py",
        "inputs": {"lesion_mask": "tests/reference/lesion_mask.nii.gz"},
        "expected": {"lesion_qc": {"compare": "exists"}},
    }
    (step_dir / "step.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (step_dir / "tests" / "make_reference.py").write_text(
        textwrap.dedent(generator_body), encoding="utf-8"
    )
    return step_dir, load_manifest(step_dir / "step.yaml")


def test_reference_generator_writes_fixtures_deterministically(tmp_path):
    step_dir, manifest = _reftest_step_dir(
        tmp_path,
        """
        import gzip
        from pathlib import Path
        out = Path("tests/reference")  # relative: the runner sets the step directory
        out.mkdir(parents=True, exist_ok=True)
        data = bytes(range(256)) * 64
        with open(out / "lesion_mask.nii.gz", "wb") as raw:
            with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as fh:
                fh.write(data)
        """,
    )
    fixture = step_dir / "tests" / "reference" / "lesion_mask.nii.gz"
    assert reftest.run_generator(manifest, step_dir) == []
    first = fixture.read_bytes()
    fixture.unlink()
    assert reftest.run_generator(manifest, step_dir) == []
    assert fixture.read_bytes() == first  # byte-identical (step-contract 1.7 item 5)


def test_reference_generator_failures_are_reported(tmp_path):
    step_dir, manifest = _reftest_step_dir(tmp_path, "pass\n")
    assert reftest.run_generator(manifest, step_dir) == [
        "fixture 'tests/reference/lesion_mask.nii.gz' does not exist"
    ]
    (step_dir / "tests" / "make_reference.py").write_text(
        "raise SystemExit(3)\n", encoding="utf-8"
    )
    problems = reftest.run_generator(manifest, step_dir)
    assert problems == ["the fixture generator exited with code 3"]
