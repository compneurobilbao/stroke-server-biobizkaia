"""Reference-test runner (contract 1.7) and orchestrator-side output checks (4.1).

Stages the manifest's ``reference_test`` inputs the way an executor does, runs the step
locally, under Docker or under Apptainer with the settings of contract section 6, then
checks the run directory as the orchestrator will (exit code, files in ``/out``,
hashes, formats, ``contracts/validate.py``) and compares outputs with ``expected``.

    stroke-step-reftest --step-dir steps/template --mode docker --image IMAGE
    stroke-step-reftest --step-dir steps/template --mode apptainer --sif step.sif
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from . import contracts, formats
from .hashing import sha256_file
from .manifest import Manifest, load_manifest

UID = 12345  # an arbitrary UID/GID with no /etc/passwd entry (contract 2.4)
GID = 12345


# --------------------------------------------------------------------------- comparisons


def _load_nifti(path: Path) -> np.ndarray:
    import nibabel as nib

    return np.asarray(nib.load(str(path)).dataobj)


def compare_dice(actual: Path, expected: Path, minimum: float) -> str | None:
    a = _load_nifti(actual) > 0
    b = _load_nifti(expected) > 0
    if a.shape != b.shape:
        return f"shape {a.shape} differs from expected {b.shape}"
    total = int(a.sum()) + int(b.sum())
    dice = 1.0 if total == 0 else 2.0 * int((a & b).sum()) / total
    if dice < minimum:
        return f"dice {dice:.4f} is below the minimum {minimum}"
    return None


def _close(x: float, y: float, rtol: float, atol: float) -> bool:
    return bool(np.isclose(x, y, rtol=rtol, atol=atol, equal_nan=True))


def compare_json(
    a: Any, b: Any, rtol: float, atol: float, where: str = "$"
) -> str | None:
    if isinstance(a, bool) or isinstance(b, bool):
        return None if a is b else f"{where}: {a!r} != {b!r}"
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return (
            None if _close(a, b, rtol, atol) else f"{where}: {a!r} not close to {b!r}"
        )
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return f"{where}: keys differ ({sorted(set(a) ^ set(b))})"
        for key in a:
            problem = compare_json(a[key], b[key], rtol, atol, f"{where}.{key}")
            if problem:
                return problem
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{where}: length {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            problem = compare_json(x, y, rtol, atol, f"{where}[{i}]")
            if problem:
                return problem
        return None
    return None if a == b else f"{where}: {a!r} != {b!r}"


def _read_tsv(path: Path) -> list[list[str]]:
    with open(path, encoding="utf-8", newline="") as fh:
        return list(csv.reader(fh, delimiter="\t"))


def compare_allclose(
    actual: Path, expected: Path, rtol: float, atol: float
) -> str | None:
    if actual.name.endswith(".nii.gz"):
        a, b = _load_nifti(actual), _load_nifti(expected)
        if a.shape != b.shape:
            return f"shape {a.shape} differs from expected {b.shape}"
        if not np.allclose(a, b, rtol=rtol, atol=atol, equal_nan=True):
            return f"maximum absolute difference {float(np.nanmax(np.abs(a - b))):.6g}"
        return None
    if actual.suffix == ".tsv":
        a_rows, b_rows = _read_tsv(actual), _read_tsv(expected)
        if len(a_rows) != len(b_rows) or (a_rows and a_rows[0] != b_rows[0]):
            return "table shape or header differs"
        for r, (ra, rb) in enumerate(zip(a_rows, b_rows)):
            for c, (x, y) in enumerate(zip(ra, rb)):
                try:
                    ok = _close(float(x), float(y), rtol, atol)
                except ValueError:
                    ok = x == y
                if not ok:
                    return f"row {r} column {c}: {x!r} differs from {y!r}"
        return None
    return "allclose is defined for NIfTI and TSV outputs only"


def compare_output(
    spec: dict, actual: Path, step_dir: Path, port_type: str
) -> str | None:
    """Apply one ``expected`` comparison; returns a problem description or None."""
    kind = spec["compare"]
    if kind == "exists":
        return None  # existence and format were checked already
    expected = step_dir / spec["file"]
    if not expected.is_file():
        return "the expected file is missing from the repository"
    if kind == "sha256":
        return (
            None
            if sha256_file(actual) == sha256_file(expected)
            else "bytes differ from the expected file"
        )
    if kind == "dice":
        return compare_dice(actual, expected, spec["min"])
    if kind == "allclose":
        return compare_allclose(
            actual, expected, spec.get("rtol", 0), spec.get("atol", 0)
        )
    if kind == "json":
        return compare_json(
            json.loads(actual.read_text(encoding="utf-8")),
            json.loads(expected.read_text(encoding="utf-8")),
            spec.get("rtol", 0),
            spec.get("atol", 0),
        )
    return f"unknown comparison '{kind}'"


# --------------------------------------------------------------------------- run directory checks


def check_run_directory(
    manifest: Manifest, in_dir: Path, out_dir: Path, exit_code: int
) -> list[str]:
    """Contract violations the orchestrator would detect (contract 4.1, provenance 2)."""
    problems: list[str] = []
    prov_path = out_dir / "provenance.json"
    err_path = out_dir / "error.json"
    allowed = {p.filename for p in manifest.outputs} | {"provenance.json", "extra"}
    if exit_code != 0:
        allowed.add("error.json")
    for entry in sorted(out_dir.iterdir()):
        if entry.name not in allowed:
            problems.append(f"file not allowed in /out: {entry.name}")
    if not prov_path.is_file():
        return problems + ["provenance.json is missing"]

    validate = contracts.validator()
    docs = [manifest.path, prov_path] + ([err_path] if err_path.is_file() else [])
    for issue in validate.validate_files(docs, strict=False):
        if issue.severity == "error":
            problems.append(f"validate.py: {issue.message}")
    prov = json.loads(prov_path.read_text(encoding="utf-8"))
    if prov.get("step") != manifest.step_ref:
        problems.append("provenance step identity differs from the manifest")
    expected_status = {0: "succeeded", 10: "rejected"}.get(exit_code, "failed")
    if prov.get("status") != expected_status:
        problems.append(
            f"provenance status '{prov.get('status')}' does not match exit code {exit_code}"
        )
    if exit_code == 10 and not err_path.is_file():
        problems.append("exit 10 without error.json")
    for label, directory in (("inputs", in_dir), ("outputs", out_dir)):
        ports = manifest.inputs if label == "inputs" else manifest.outputs
        by_name = {p.name: p for p in ports}
        for rec in prov.get(label, []):
            port = by_name.get(rec["port"])
            path = directory / port.filename if port else None
            if path is None or not path.is_file():
                problems.append(f"{label}: file of port '{rec['port']}' is missing")
            elif (
                sha256_file(path) != rec["sha256"]
                or path.stat().st_size != rec["size_bytes"]
            ):
                problems.append(
                    f"{label}: hash or size of port '{rec['port']}' differs from provenance"
                )
    if exit_code == 0:
        for port in manifest.outputs:
            path = out_dir / port.filename
            if path.is_file():
                problems.extend(
                    f"output '{port.name}': {p.message}"
                    for p in formats.check_file(path, port.type)
                )
            elif port.required:
                problems.append(f"required output '{port.name}' is missing")
    return problems


# --------------------------------------------------------------------------- execution


def _safe_fixture(step_dir: Path, rel: str) -> Path:
    path = (step_dir / rel).resolve()
    if step_dir.resolve() not in path.parents:
        raise ValueError(f"fixture path leaves the step directory: {rel}")
    return path


def executor_env(manifest: Manifest) -> dict[str, str]:
    """The environment an executor provides (contract 2.3)."""
    cpus = str(manifest.resources["cpus"])
    env = {
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "TZ": "UTC",
        "STEP_CPUS": cpus,
        "STEP_MEMORY_MB": str(manifest.resources["memory_mb"]),
        "STEP_GPUS": "0",
    }
    for name in (
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS",
    ):
        env[name] = cpus
    return env


def stage_inputs(manifest: Manifest, step_dir: Path, in_dir: Path) -> None:
    rt = manifest.data["reference_test"]
    for port_name, rel in rt["inputs"].items():
        port = manifest.input_port(port_name)
        shutil.copyfile(_safe_fixture(step_dir, rel), in_dir / port.filename)
    validate = contracts.validator()
    values = validate.effective_parameters(
        manifest.parameters_schema, rt.get("parameters", {})
    )
    (in_dir / "params.json").write_text(json.dumps(values, indent=2), encoding="utf-8")


def build_command(
    mode: str,
    manifest: Manifest,
    step_dir: Path,
    work: Path,
    image: str | None,
    sif: str | None,
    bundle_dirs: dict[str, Path],
) -> tuple[list[str], dict[str, str] | None]:
    env = executor_env(manifest)
    step_args = ["--in", "/in", "--out", "/out", "--params", "/in/params.json"]
    in_dir, out_dir = work / "in", work / "out"
    resources = manifest.resources
    if mode == "docker":
        cmd = [
            "docker", "run", "--rm", "--read-only",
            "--user", f"{UID}:{GID}",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--network", "none", "--pids-limit", "512",
            "--memory", f"{resources['memory_mb']}m",
            "--cpus", str(resources["cpus"]),
            "--tmpfs", f"/tmp:rw,mode=1777,size={resources.get('scratch_mb', 1024)}m",
            "-v", f"{in_dir}:/in:ro", "-v", f"{out_dir}:/out",
        ]  # fmt: skip
        for name, directory in bundle_dirs.items():
            cmd += ["-v", f"{directory}:/ref/{name}:ro"]
        for key, value in env.items():
            cmd += ["-e", f"{key}={value}"]
        cmd += ["--entrypoint", "/usr/local/bin/run", image or "", *step_args]
        return cmd, None
    if mode == "apptainer":
        cmd = [
            "apptainer", "exec", "--containall", "--cleanenv", "--no-home",
            "--bind", f"{in_dir}:/in:ro", "--bind", f"{out_dir}:/out",
        ]  # fmt: skip
        for name, directory in bundle_dirs.items():
            cmd += ["--bind", f"{directory}:/ref/{name}:ro"]
        for key, value in env.items():
            cmd += ["--env", f"{key}={value}"]
        cmd += [sif or "", "/usr/local/bin/run", *step_args]
        return cmd, None
    # local: run the step's bin/run with the working tree, outside any container
    local_env = dict(os.environ)
    local_env.update(env)
    local_env.update(
        TMPDIR=str(work / "tmp"),
        HOME=str(work / "tmp"),
        PYTHONPATH=os.pathsep.join(
            [str(step_dir / "src")]
            + ([os.environ["PYTHONPATH"]] if "PYTHONPATH" in os.environ else [])
        ),
    )
    cmd = [
        sys.executable, str(step_dir / "bin" / "run"),
        "--in", str(in_dir), "--out", str(out_dir),
        "--params", str(in_dir / "params.json"),
        "--manifest", str(step_dir / "step.yaml"),
    ]  # fmt: skip
    return cmd, local_env


def run_reference_test(
    step_dir: Path,
    mode: str,
    image: str | None = None,
    sif: str | None = None,
    workdir: Path | None = None,
    bundle_dirs: dict[str, Path] | None = None,
    save_provenance: Path | None = None,
) -> int:
    step_dir = step_dir.resolve()
    manifest = load_manifest(step_dir / "step.yaml")
    rt = manifest.data.get("reference_test")
    if not rt:
        print("FAIL: the manifest has no reference_test")
        return 1
    bundle_dirs = bundle_dirs or {}
    missing = [n for n in rt.get("bundles", {}) if n not in bundle_dirs]
    if missing:
        print(f"FAIL: reference bundles not supplied: {', '.join(missing)}")
        return 1

    work = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="reftest-"))
    for sub in ("in", "out", "tmp"):
        if (work / sub).exists():
            shutil.rmtree(work / sub)
        (work / sub).mkdir(parents=True)
    for sub in ("out", "tmp"):
        os.chmod(work / sub, 0o777)  # the step runs under an arbitrary UID
    stage_inputs(manifest, step_dir, work / "in")

    cmd, env = build_command(mode, manifest, step_dir, work, image, sif, bundle_dirs)
    print("running:", " ".join(cmd), flush=True)
    timeout = manifest.resources["wall_time_minutes"] * 60
    proc = subprocess.run(cmd, env=env, timeout=timeout, check=False)
    print(f"step exit code: {proc.returncode}", flush=True)

    problems = check_run_directory(manifest, work / "in", work / "out", proc.returncode)
    if proc.returncode != 0:
        problems.append(f"the reference test must exit 0, got {proc.returncode}")
    else:
        for port_name, spec in rt["expected"].items():
            port = manifest.output_port(port_name)
            actual = work / "out" / port.filename
            if not actual.is_file():
                problems.append(f"expected output '{port_name}' was not produced")
                continue
            problem = compare_output(spec, actual, step_dir, port.type)
            if problem:
                problems.append(f"output '{port_name}' ({spec['compare']}): {problem}")
    prov = work / "out" / "provenance.json"
    if save_provenance and prov.is_file():
        save_provenance.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(prov, save_provenance)
    if problems:
        for p in problems:
            print(f"FAIL: {p}")
        return 1
    print(f"PASS: reference test of {manifest.name} {manifest.version} ({mode})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a step's reference test.")
    parser.add_argument("--step-dir", type=Path, required=True)
    parser.add_argument(
        "--mode", choices=["local", "docker", "apptainer"], required=True
    )
    parser.add_argument("--image", help="Docker image (mode docker)")
    parser.add_argument("--sif", help="SIF file (mode apptainer)")
    parser.add_argument("--workdir", type=Path, help="directory for in/, out/ and tmp/")
    parser.add_argument("--bundle", action="append", default=[], metavar="NAME=DIR")
    parser.add_argument("--save-provenance", type=Path)
    args = parser.parse_args(argv)
    if args.mode == "docker" and not args.image:
        parser.error("--image is required for mode docker")
    if args.mode == "apptainer" and not args.sif:
        parser.error("--sif is required for mode apptainer")
    bundles = {}
    for item in args.bundle:
        name, _, directory = item.partition("=")
        bundles[name] = Path(directory).resolve()
    return run_reference_test(
        args.step_dir,
        args.mode,
        args.image,
        args.sif,
        args.workdir,
        bundles,
        args.save_provenance,
    )


if __name__ == "__main__":
    sys.exit(main())
