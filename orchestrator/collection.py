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

Deep format checks of output files against their artefact type (NIfTI grid,
binary values) are not done here; see the follow-ups of card F0-15.

Executor outcome ``terminated`` maps to ``failed`` (provenance.md 3.2). Because
the outcome after ``SIGTERM`` is decided by the executor, contract 1.1 rules on
cancellation do not change this module.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from contracts import validate as contract
from orchestrator.executors.base import BundleMount, Outcome, StepResult
from orchestrator.pipeline import condition_holds, staged_name
from orchestrator.store import sha256_file

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
    return Verdict(
        "succeeded", outputs=outputs, provenance=provenance, provenance_sha256=prov_sha
    )
