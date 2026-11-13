"""Synthetic reference dataset of the lesion-qc step (nothing is committed).

A ball of ones in the left hemisphere on the 2 mm MNI152 working grid. The expected
lesion_qc.json is derived with plain numpy, independently of the step.

    python steps/lesion-qc/tests/make_reference.py [DEST_DIR]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml

CENTRE = (60, 60, 40)
RADIUS = 6.0


def working_grid(registry_path: Path):
    grid = yaml.safe_load(registry_path.read_text(encoding="utf-8"))["grids"][
        "MNI152NLin6Asym-res2"
    ]
    return tuple(grid["shape"]), np.array(grid["affine"], dtype=float)


def ball(shape, centre=CENTRE, radius=RADIUS) -> np.ndarray:
    ii, jj, kk = np.indices(shape)
    d2 = (ii - centre[0]) ** 2 + (jj - centre[1]) ** 2 + (kk - centre[2]) ** 2
    return (d2 <= radius**2).astype(np.uint8)


def save_mni(path: Path, data: np.ndarray, affine: np.ndarray, code: int = 4) -> None:
    img = nib.Nifti1Image(data, affine)
    img.set_sform(affine, code=code)
    img.set_qform(affine, code=code)
    nib.save(img, str(path))


def expected_for(mask: np.ndarray, affine: np.ndarray) -> dict:
    idx = np.argwhere(mask)
    world = idx @ affine[:3, :3].T + affine[:3, 3]
    n = len(idx)
    vol = n * 8.0 / 1000.0
    left = int((world[:, 0] < -1).sum())
    return {
        "schema_version": "1.0",
        "mode": "mni_mask",
        "input_space": "MNI152NLin6Asym",
        "grid": {
            "name": "MNI152NLin6Asym-res2",
            "shape": list(mask.shape),
            "voxel_size_mm": [2.0, 2.0, 2.0],
            "source_shape": list(mask.shape),
            "source_voxel_size_mm": [2.0, 2.0, 2.0],
            "resampled": False,
        },
        "n_voxels": n,
        "volume_ml": vol,
        "volume_source_ml": vol,
        "hemisphere": {
            "label": "left",
            "left_voxels": left,
            "right_voxels": 0,
            "midline_voxels": n - left,
        },
        "components": {
            "count": 1,
            "connectivity": 26,
            "volumes_ml": [vol],
            "largest_fraction": 1.0,
        },
        "bounding_box": {
            "voxel": {"min": idx.min(axis=0).tolist(), "max": idx.max(axis=0).tolist()},
            "world_mm": {"min": world.min(axis=0).tolist(), "max": world.max(axis=0).tolist()},
        },
        "centroid_mm": world.mean(axis=0).tolist(),
        "warnings": [],
        "thresholds": {
            "warn_below_ml": 0.5,
            "max_volume_ml": 800.0,
            "reject_bilateral": False,
            "bilateral_min_fraction": 0.05,
        },
    }


def generate(dest: Path, registry_path: Path) -> dict:
    shape, affine = working_grid(registry_path)
    mask = ball(shape)
    (dest / "expected").mkdir(parents=True, exist_ok=True)
    save_mni(dest / "lesion_mni.nii.gz", mask, affine)
    expected = expected_for(mask, affine)
    (dest / "expected" / "lesion_qc.json").write_text(
        json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return expected


def main() -> None:
    here = Path(__file__).resolve().parent
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "reference"
    registry = here.parents[2] / "contracts" / "artefact-types.yaml"
    print(json.dumps(generate(dest, registry))[:200])


if __name__ == "__main__":
    main()
