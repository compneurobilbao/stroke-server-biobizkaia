"""Check a finished attempt against the step contract (step-contract.md 4.1, provenance.md 2).

``assess`` turns an executor ``StepResult`` into the attempt's final
``outcome`` (the six values of ``step_runs.outcome``) plus what must be stored.
The executor's own classification is taken as is (exit 0 / 10 / other, and
``canceled`` or ``timed_out`` when the platform stopped the attempt); the
orchestrator only *downgrades* a ``succeeded`` or ``rejected`` attempt to
``contract_violation`` when the files it left do not satisfy the contract:

- exit 0: every required output present (the manifest's ``required`` outputs and
  the slot's guaranteed ports whose ``when`` holds); no file in ``/out`` other
  than declared ``<port><ext>`` outputs, ``provenance.json``, ``error.json`` and
  ``extra/``, and no symbolic link anywhere at the top of ``/out``
  (a link could point to any host file); ``provenance.json`` present, schema-valid, ``StepProvenance`` of
  the step that was started, ``status: succeeded``; SHA-256 and size of every
  staged input and every output equal to the record; bundles reported equal to
  the bundles mounted.
- exit 10: ``error.json`` present, schema-valid, category ``input_rejected`` or
  ``qc_failed`` and a code declared in the manifest's ``errors[]``.

- exit 0, NIfTI outputs whose artefact type states constraints in the registry
  (``nifti.grid``, ``nifti.binary``; artefact-types.md sections 2 and 3): a
  readable gzip-compressed NIfTI-1/2 file; for ``grid``, shape equal to the
  grid's and affine within 1e-4 mm of it (header only); for ``binary``, every
  value (after the header's scaling) is 0 or 1, checked in one streaming pass
  over the voxel data with bounded memory. Types without such constraints and
  other formats (JSON schemas, TSV columns, PDF/PNG signatures) are not checked
  here. These checks run last, after the hash checks, so that only files the
  step declared and recorded are parsed.

Executor outcome ``terminated`` maps to ``failed`` (provenance.md 3.2). Because
the outcome after ``SIGTERM`` is decided by the executor, contract 1.1 rules on
cancellation do not change this module.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import zlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts import validate as contract
from orchestrator.executors.base import BundleMount, Outcome, StepResult
from orchestrator.pipeline import (
    artefact_grids,
    artefact_types,
    condition_holds,
    staged_name,
)
from orchestrator.store import sha256_file

GRID_TOLERANCE_MM = 1e-4  # artefact-types.md section 2
GZIP_MAGIC = b"\x1f\x8b"
SCAN_CHUNK_BYTES = 8 << 20  # voxel data is checked in chunks of this size

RESERVED = {"provenance.json", "error.json", "extra"}


@dataclass
class Verdict:
    outcome: str  # step_runs.outcome
    failure_code: str | None = None
    failure_message: str | None = None
    outputs: dict[str, Path] = field(default_factory=dict)  # port -> collected file
    provenance: dict[str, Any] | None = None
    provenance_sha256: str | None = None
    error: dict[str, Any] | None = None

    @property
    def status(self) -> str:
        return {
            "succeeded": "succeeded",
            "canceled": "canceled",
        }.get(self.outcome, "failed")


def _violation(code: str, message: str, **kw) -> Verdict:
    return Verdict(
        outcome="contract_violation",
        failure_code=f"contract_violation_{code}",
        failure_message=message,
        **kw,
    )


def _read_record(path: Path) -> tuple[dict | None, str | None]:
    # A symlink could point anywhere on the host: never read through it.
    if path.is_symlink() or not path.is_file():
        return None, None
    raw = path.read_bytes()
    try:
        data = json.loads(raw)
    except ValueError:
        return None, None
    return (data if isinstance(data, dict) else None), hashlib.sha256(raw).hexdigest()


def _valid_record(data: dict | None, schema: str) -> bool:
    if data is None or contract.schema_errors(data, schema):
        return False
    checker = (
        contract.check_provenance if schema == "provenance" else contract.check_error
    )
    return not checker(data)


def assess(
    result: StepResult,
    *,
    manifest: Mapping[str, Any],
    slot: Mapping[str, Any],
    present_inputs: set[str],
    staged_inputs: Mapping[str, tuple[str, str, int]],
    mounted: list[BundleMount],
) -> Verdict:
    """Final outcome of one attempt.

    ``staged_inputs`` maps input port -> (artefact type, sha256, size) of what
    was staged under ``/in``.
    """
    out_dir = result.out_dir
    provenance, prov_sha = _read_record(out_dir / "provenance.json")
    error, _ = _read_record(out_dir / "error.json")
    stored_error = error if _valid_record(error, "error") else None

    if result.outcome is Outcome.CANCELED:
        return Verdict("canceled", "canceled", "The step was canceled.")
    if result.outcome is Outcome.TIMED_OUT:
        return Verdict("timed_out", "timed_out", "The step exceeded its time limit.")
    if result.outcome in (Outcome.FAILED, Outcome.TERMINATED):
        return Verdict(
            "failed",
            "step_failed",
            # Executor messages may name host paths; users get a generic text.
            f"The step failed (exit code {result.exit_code}).",
            provenance=provenance if _valid_record(provenance, "provenance") else None,
            provenance_sha256=prov_sha
            if _valid_record(provenance, "provenance")
            else None,
            error=stored_error,
        )
    if result.outcome in (Outcome.SUCCEEDED, Outcome.REJECTED) and any(
        (out_dir / name).is_symlink() for name in RESERVED
    ):
        return _violation("symlink", "Symbolic link in /out is not allowed.")
    if result.outcome is Outcome.REJECTED:
        declared = {e["code"] for e in manifest.get("errors", [])}
        if (
            stored_error is None
            or stored_error["category"] not in ("input_rejected", "qc_failed")
            or stored_error["code"] not in declared
        ):
            return _violation(
                "error_record", "Exit code 10 without a valid, declared error.json."
            )
        keep = _valid_record(provenance, "provenance")
        return Verdict(
            "rejected",
            None,
            None,
            provenance=provenance if keep else None,
            provenance_sha256=prov_sha if keep else None,
            error=stored_error,
        )
    return _assess_success(
        result,
        manifest,
        slot,
        present_inputs,
        staged_inputs,
        mounted,
        provenance,
        prov_sha,
    )


def _assess_success(
    result, manifest, slot, present_inputs, staged_inputs, mounted, provenance, prov_sha
) -> Verdict:
    out_dir: Path = result.out_dir
    declared = {o["name"]: o for o in manifest["outputs"]}
    by_file = {staged_name(name, o["type"]): name for name, o in declared.items()}

    outputs: dict[str, Path] = {}
    if out_dir.is_dir():
        for entry in sorted(out_dir.iterdir()):
            if entry.is_symlink():
                return _violation(
                    "symlink", f"Symbolic link in /out is not allowed: {entry.name}."
                )
            if entry.name == "extra" and entry.is_dir():
                continue
            if entry.name in RESERVED and entry.is_file():
                continue
            port = by_file.get(entry.name)
            if port is None or not entry.is_file():
                return _violation(
                    "unexpected_file", f"Undeclared file in /out: {entry.name}."
                )
            outputs[port] = entry

    required = {name for name, o in declared.items() if o.get("required", True)}
    required |= {
        p["port"]
        for p in slot["produces"]
        if not p.get("optional", False)
        and condition_holds(p.get("when"), present_inputs)
    }
    missing = sorted(required - set(outputs))
    if missing:
        return _violation(
            "missing_output", f"Required outputs missing: {', '.join(missing)}."
        )

    if (
        not _valid_record(provenance, "provenance")
        or provenance.get("kind") != "StepProvenance"
    ):
        return _violation("provenance", "provenance.json is missing or invalid.")
    if provenance["step"] != {"name": manifest["name"], "version": manifest["version"]}:
        return _violation("provenance", "provenance.json names another step.")
    if provenance["status"] != "succeeded":
        return _violation("provenance", "provenance.json status is not 'succeeded'.")

    recorded_out = {r["port"]: r for r in provenance["outputs"]}
    if set(recorded_out) != set(outputs):
        return _violation("hash_mismatch", "provenance.json outputs differ from /out.")
    for port, path in outputs.items():
        rec = recorded_out[port]
        if (
            rec["type"] != declared[port]["type"]
            or rec["sha256"] != sha256_file(path)
            or rec["size_bytes"] != path.stat().st_size
        ):
            return _violation(
                "hash_mismatch", f"Output '{port}' differs from provenance.json."
            )
    recorded_in = {r["port"]: r for r in provenance["inputs"]}
    for port, (atype, sha, size) in staged_inputs.items():
        rec = recorded_in.get(port)
        if rec is None or (rec["type"], rec["sha256"], rec["size_bytes"]) != (
            atype,
            sha,
            size,
        ):
            return _violation(
                "hash_mismatch", f"Input '{port}' differs from provenance.json."
            )
    recorded_bundles = {b["name"]: b for b in provenance["bundles"]}
    for mount in mounted:
        rec = recorded_bundles.get(mount.name)
        if rec is None or (rec["id"], rec["version"], rec["content_digest"]) != (
            mount.id,
            mount.version,
            mount.content_digest,
        ):
            return _violation(
                "bundle_mismatch",
                f"Bundle '{mount.name}' differs from provenance.json.",
            )
    _version, types = artefact_types()
    for port, path in sorted(outputs.items()):
        problem = check_output_format(path, types[declared[port]["type"]], port)
        if problem is not None:
            return _violation(*problem)
    return Verdict(
        "succeeded", outputs=outputs, provenance=provenance, provenance_sha256=prov_sha
    )


# ------------------------------------------------------------- format checks


def check_output_format(
    path: Path, type_def: Mapping[str, Any], port: str
) -> tuple[str, str] | None:
    """``(code, message)`` of the first registry constraint ``path`` violates, or None.

    Only NIfTI types with ``nifti.grid`` or ``nifti.binary`` in the registry
    are checked (see the module docstring). ``code`` is the suffix of
    ``contract_violation_<code>``: ``nifti_unreadable``, ``nifti_grid`` or
    ``nifti_binary``. Messages name the port, never a path.
    """
    if type_def.get("format") != "nifti":
        return None
    constraints = type_def.get("nifti") or {}
    grid_name = constraints.get("grid")
    binary = bool(constraints.get("binary"))
    if not grid_name and not binary:
        return None
    unreadable = (
        "nifti_unreadable",
        f"Output '{port}' is not a readable gzip-compressed NIfTI image.",
    )
    import nibabel as nib  # lazily: nibabel is heavy and only needed here
    import numpy as np

    with path.open("rb") as handle:
        if handle.read(2) != GZIP_MAGIC:
            return unreadable
    try:
        img = nib.load(str(path))
    except Exception:  # noqa: BLE001 - nibabel raises many types for corrupt files
        return unreadable
    if not isinstance(img, (nib.Nifti1Image, nib.Nifti2Image)):
        return unreadable

    if grid_name:
        grid = artefact_grids()[grid_name]
        shape = [int(s) for s in img.shape]
        if shape != list(grid["shape"]):
            return (
                "nifti_grid",
                f"Output '{port}' is not on the {grid_name} grid: shape "
                f"{'x'.join(map(str, shape))}, expected "
                f"{'x'.join(map(str, grid['shape']))}.",
            )
        if not np.allclose(
            np.asarray(img.affine, dtype=float),
            np.asarray(grid["affine"], dtype=float),
            rtol=0,
            atol=GRID_TOLERANCE_MM,
        ):
            return (
                "nifti_grid",
                f"Output '{port}' is not on the {grid_name} grid: its affine differs.",
            )
    if binary:
        values = _binary_scan(path, img)
        if values is None:
            return unreadable
        if not values:
            return (
                "nifti_binary",
                f"Output '{port}' must contain only the values 0 and 1.",
            )
    return None


def _binary_scan(path: Path, img: Any) -> bool | None:
    """Whether every voxel value of ``img`` is 0 or 1; ``None`` if the data is unreadable.

    Streams the decompressed voxel data in chunks of ``SCAN_CHUNK_BYTES`` (one
    pass, bounded memory) instead of loading the image: a native-space mask can
    be large. Layout and scaling are taken from nibabel's array proxy (data
    offset, on-disk dtype with byte order, ``scl_slope``/``scl_inter``), so the
    values are those nibabel and the step SDK would see; NaN is not binary.
    """
    import numpy as np

    proxy = img.dataobj
    dtype = np.dtype(proxy.dtype)
    if dtype.fields is not None:  # RGB and other structured types
        return False
    slope, inter = float(proxy.slope), float(proxy.inter)
    scaled = (slope, inter) != (1.0, 0.0)
    itemsize = dtype.itemsize
    remaining = int(np.prod(proxy.shape, dtype=np.int64)) * itemsize
    step = max(1, SCAN_CHUNK_BYTES // itemsize) * itemsize
    try:
        with gzip.open(path, "rb") as handle:
            handle.seek(int(proxy.offset))
            while remaining > 0:
                want = min(remaining, step)
                raw = handle.read(want)
                if len(raw) != want:
                    return None  # truncated
                values = np.frombuffer(raw, dtype=dtype)
                if scaled:
                    values = values.astype(np.float64) * slope + inter
                if not ((values == 0) | (values == 1)).all():
                    return False
                remaining -= want
    except (OSError, EOFError, zlib.error):
        return None
    return True
