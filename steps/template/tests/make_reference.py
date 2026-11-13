"""Generate the synthetic reference dataset of the template step.

Nothing here is real data: a ball of ones on the 2 mm MNI152 working grid. The expected
measurement is derived from the voxel count with plain numpy, independently of the step.

    python steps/template/tests/make_reference.py [DEST_DIR]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml

CENTRE = (30, 60, 40)  # voxel indices
RADIUS = 6.0  # voxels


def working_grid(registry_path: Path) -> tuple[tuple[int, ...], np.ndarray]:
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    grid = registry["grids"]["MNI152NLin6Asym-res2"]
    return tuple(grid["shape"]), np.array(grid["affine"], dtype=float)


def ball(shape: tuple[int, ...], centre=CENTRE, radius=RADIUS) -> np.ndarray:
    ii, jj, kk = np.indices(shape)
    dist2 = (ii - centre[0]) ** 2 + (jj - centre[1]) ** 2 + (kk - centre[2]) ** 2
    return (dist2 <= radius**2).astype(np.uint8)


def generate(dest: Path, registry_path: Path) -> dict:
    """Write ``lesion_mask.nii.gz`` and ``expected/lesion_qc.json`` into ``dest``."""
    shape, affine = working_grid(registry_path)
    mask = ball(shape)
    (dest / "expected").mkdir(parents=True, exist_ok=True)
    nib.save(nib.Nifti1Image(mask, affine), str(dest / "lesion_mask.nii.gz"))
    voxel_mm3 = abs(float(np.linalg.det(affine[:3, :3])))
    n = int(mask.sum())
    expected = {
        "n_voxels": n,
        "voxel_volume_mm3": voxel_mm3,
        "volume_ml": n * voxel_mm3 / 1000.0,
    }
    (dest / "expected" / "lesion_qc.json").write_text(
        json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return expected


def main() -> None:
    here = Path(__file__).resolve().parent
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "reference"
    registry = here.parents[2] / "contracts" / "artefact-types.yaml"
    print(json.dumps(generate(dest, registry)))


if __name__ == "__main__":
    main()
