"""Format checks against the artefact type registry (artefact-types.md section 3)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from jsonschema import Draft202012Validator

from . import contracts

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PDF_SIGNATURE = b"%PDF-"
GRID_TOLERANCE_MM = 1e-4


@dataclass
class FormatProblem:
    """One violated format rule. ``message`` is safe to show (no paths, no identifiers)."""

    message: str
    details: dict[str, Any] = field(default_factory=dict)


def check_file(path: Path, type_name: str) -> list[FormatProblem]:
    """Check one file against its artefact type; empty list means it conforms."""
    registry = contracts.registry()
    spec = registry["types"][type_name]
    fmt = spec["format"]
    if fmt == "nifti":
        return _check_nifti(path, spec, registry["grids"])
    if fmt == "json":
        return _check_json(path, spec)
    if fmt == "tsv":
        return _check_tsv(path, spec)
    if fmt == "png":
        return _check_signature(path, PNG_SIGNATURE, "PNG")
    if fmt == "pdf":
        return _check_signature(path, PDF_SIGNATURE, "PDF")
    return [FormatProblem(f"unsupported format '{fmt}'")]


def _check_signature(path: Path, signature: bytes, label: str) -> list[FormatProblem]:
    with open(path, "rb") as fh:
        head = fh.read(len(signature))
    if head != signature:
        return [FormatProblem(f"The file is not a valid {label} file.")]
    return []


def _check_nifti(path: Path, spec: dict, grids: dict) -> list[FormatProblem]:
    import nibabel as nib  # imported lazily: nibabel is heavy

    with open(path, "rb") as fh:
        magic = fh.read(2)
    if magic != b"\x1f\x8b":
        return [FormatProblem("The file is not a gzip-compressed NIfTI image.")]
    try:
        img = nib.load(str(path))
    except Exception:  # noqa: BLE001 - nibabel raises many types for corrupt files
        return [FormatProblem("The file is not a readable NIfTI-1 or NIfTI-2 image.")]
    if not isinstance(img, (nib.Nifti1Image, nib.Nifti2Image)):
        return [FormatProblem("The file is not a NIfTI-1 or NIfTI-2 image.")]

    constraints = spec.get("nifti", {})
    problems: list[FormatProblem] = []
    grid_name = constraints.get("grid")
    if grid_name:
        grid = grids[grid_name]
        shape = [int(s) for s in img.shape]
        affine = np.asarray(img.affine, dtype=float)
        expected = np.asarray(grid["affine"], dtype=float)
        if shape != list(grid["shape"]):
            problems.append(
                FormatProblem(
                    f"The image is not on the {grid_name} voxel grid: expected shape "
                    f"{list(grid['shape'])}, got {shape}.",
                    {"shape": "x".join(map(str, shape))},
                )
            )
        elif not np.allclose(affine, expected, rtol=0, atol=GRID_TOLERANCE_MM):
            problems.append(
                FormatProblem(
                    f"The image affine does not match the {grid_name} voxel grid."
                )
            )
    if constraints.get("binary"):
        try:
            data = np.asanyarray(img.dataobj)
        except Exception:  # noqa: BLE001
            return problems + [FormatProblem("The image data cannot be read.")]
        bad = ~np.isin(data, (0, 1))  # NaN is not in (0, 1), so it counts as non-binary
        if bad.any():
            distinct = int(np.unique(data[np.isfinite(data)]).size)
            problems.append(
                FormatProblem(
                    "The mask must contain only the values 0 and 1.",
                    {
                        "distinct_values": distinct,
                        "non_binary_voxels": int(bad.sum()),
                    },
                )
            )
    return problems


def _json_validator(spec: dict) -> Draft202012Validator | None:
    rel = spec.get("json_schema")
    if not rel:
        return None
    base = contracts.contracts_dir()
    schema_path = base / rel
    validate = contracts.validator()
    for key, filename in validate.SCHEMA_FILES.items():
        if schema_path.name == filename and schema_path.parent == validate.SCHEMA_DIR:
            return validate.validator_for(key)
    return Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))


def _check_json(path: Path, spec: dict) -> list[FormatProblem]:
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [FormatProblem("The file is not valid UTF-8 JSON.")]
    validator = _json_validator(spec)
    if validator is not None:
        errors = sorted(validator.iter_errors(document), key=lambda e: list(e.path))
        if errors:
            return [
                FormatProblem(
                    "The JSON document does not satisfy its schema.",
                    {
                        "first_error": errors[0].message[:200],
                        "error_count": len(errors),
                    },
                )
            ]
    return []


def _check_tsv(path: Path, spec: dict) -> list[FormatProblem]:
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return [FormatProblem("The file is not UTF-8 text.")]
    header = text.splitlines()[0].split("\t") if text.strip() else []
    missing = [c for c in spec.get("tsv_columns", []) if c not in header]
    if missing:
        return [
            FormatProblem(
                "The table lacks required columns: " + ", ".join(missing) + ".",
                {"missing_columns": ",".join(missing)},
            )
        ]
    if not spec.get("tsv_columns") and not header:
        return [FormatProblem("The table has no header row.")]
    return []


__all__ = ["FormatProblem", "check_file"]
