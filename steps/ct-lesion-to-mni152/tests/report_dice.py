"""Dice of a step output mask against the fixture ground truth (both on the 2 mm grid).

    python step/tests/report_dice.py OUT_DIR TRUTH_MASK
Prints one JSON line; used by CI to report the real value.
"""

import json
import sys
from pathlib import Path

import nibabel as nib
import numpy as np


def dice(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a > 0, b > 0
    total = int(a.sum()) + int(b.sum())
    return 1.0 if total == 0 else 2.0 * int((a & b).sum()) / total


def centroid_mm(img: nib.Nifti1Image) -> list:
    idx = np.argwhere(np.asanyarray(img.dataobj) > 0)
    return [round(float(v), 1) for v in nib.affines.apply_affine(img.affine, idx.mean(axis=0))]


if __name__ == "__main__":
    out = nib.load(str(Path(sys.argv[1]) / "lesion_mask_mni.nii.gz"))
    truth = nib.load(sys.argv[2])
    a, b = np.asanyarray(out.dataobj), np.asanyarray(truth.dataobj)
    print(
        json.dumps(
            {
                "dice": round(dice(a, b), 4),
                "dice_if_left_right_mirrored": round(dice(a[::-1], b), 4),
                "voxels_out": int((a > 0).sum()),
                "voxels_truth": int((b > 0).sum()),
                "centroid_mm_out": centroid_mm(out),
                "centroid_mm_truth": centroid_mm(truth),
                "centroid_error_mm": round(float(np.linalg.norm(np.array(centroid_mm(out)) - np.array(centroid_mm(truth)))), 2),
            }
        )
    )
