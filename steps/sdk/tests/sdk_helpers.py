"""Helpers shared by the SDK tests: a small step manifest and a staged run directory."""

from __future__ import annotations

import json
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml
from stroke_step_sdk import contracts, run_step

MANIFEST = {
    "contract_version": "1.0",
    "kind": "Step",
    "name": "sdk-test-step",
    "version": "1.0.0",
    "title": "SDK test step",
    "provides": "test-slot",
    "image": {"repository": "docker.io/example/sdk-test-step"},
    "licence": {"spdx": "MIT"},
    "inputs": [
        {"name": "lesion_mask", "type": "lesion_mask_mni"},
        {"name": "ct", "type": "ct_native", "required": False},
    ],
    "outputs": [
        {"name": "lesion_qc", "type": "lesion_qc"},
        {"name": "qc_figure", "type": "qc_figure", "required": False},
    ],
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "threshold": {"type": "number", "minimum": 0, "default": 1.0},
            "seed": {"type": "integer", "default": 7},
        },
    },
    "bundles": [
        {"name": "atlas", "type": "normative-connectome", "required": False},
    ],
    "resources": {"cpus": 1, "memory_mb": 256, "wall_time_minutes": 1},
    "errors": [
        {
            "code": "input_missing",
            "category": "input_rejected",
            "description": "A required input is absent.",
        },
        {
            "code": "input_format_invalid",
            "category": "input_rejected",
            "description": "An input file is invalid.",
        },
        {
            "code": "invalid_parameters",
            "category": "input_rejected",
            "description": "Parameters are invalid.",
        },
        {
            "code": "mask_empty",
            "category": "input_rejected",
            "description": "The mask is empty.",
        },
        {
            "code": "low_quality",
            "category": "qc_failed",
            "description": "A QC check failed.",
        },
    ],
}


def mni2_grid() -> tuple[tuple[int, ...], np.ndarray]:
    grid = contracts.registry()["grids"]["MNI152NLin6Asym-res2"]
    return tuple(grid["shape"]), np.array(grid["affine"], dtype=float)


def write_mask(path: Path, values: np.ndarray | None = None, affine=None) -> None:
    shape, grid_affine = mni2_grid()
    if values is None:
        values = np.zeros(shape, dtype=np.uint8)
        values[40:46, 50:56, 40:46] = 1
    img = nib.Nifti1Image(values, grid_affine if affine is None else affine)
    nib.save(img, str(path))


class Run:
    """A staged run directory with ``in/``, ``out/``, ``tmp/`` and a manifest."""

    def __init__(self, root: Path, manifest: dict) -> None:
        self.root = root
        self.in_dir = root / "in"
        self.out_dir = root / "out"
        self.manifest_path = root / "step.yaml"
        for d in (self.in_dir, self.out_dir, root / "tmp"):
            d.mkdir()
        self.manifest_path.write_text(
            yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
        )
        self.write_params({})
        write_mask(self.in_dir / "lesion_mask.nii.gz")
        self.env = {
            "STEP_CPUS": "1",
            "TMPDIR": str(root / "tmp"),
            "HOME": str(root / "tmp"),
        }

    def write_params(self, params: dict) -> None:
        (self.in_dir / "params.json").write_text(json.dumps(params), encoding="utf-8")

    def run(self, fn, ref_dir: Path | None = None) -> int:
        return run_step(
            fn,
            in_dir=self.in_dir,
            out_dir=self.out_dir,
            manifest_path=self.manifest_path,
            ref_dir=ref_dir or self.root / "ref",
            environ=self.env,
        )

    def json(self, name: str) -> dict:
        return json.loads((self.out_dir / name).read_text(encoding="utf-8"))

    def files(self) -> set[str]:
        return {p.name for p in self.out_dir.iterdir()}

    def clear_out(self) -> None:
        for p in self.out_dir.iterdir():
            p.unlink()

    def validate(self) -> list[str]:
        """Errors reported by contracts/validate.py for manifest + provenance (+ error)."""
        docs = [self.manifest_path, self.out_dir / "provenance.json"]
        if (self.out_dir / "error.json").exists():
            docs.append(self.out_dir / "error.json")
        issues = contracts.validator().validate_files(docs, strict=False)
        return [i.message for i in issues if i.severity == "error"]


def write_qc(ctx, volume: float = 1.0) -> None:
    ctx.output_path("lesion_qc").write_text(
        json.dumps({"volume_ml": volume}), encoding="utf-8"
    )
