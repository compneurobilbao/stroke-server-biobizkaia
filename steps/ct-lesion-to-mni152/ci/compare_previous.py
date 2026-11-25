"""Compare the outputs of this image with those of the previously published image (1.0.0).

    python steps/ct-lesion-to-mni152/ci/compare_previous.py NEW_OUT PREVIOUS_OUT

Both directories are /out of a run on the same inputs and parameters. The lesion mask on the
working grid must be identical (values and affine); the registered CT must be identical up to
1e-4 HU (float32 output of the same computation). The QC figure is not compared (PNG bytes
depend on nothing but the data, but its check is visual). Prints one JSON line and exits 1 on
any difference.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import nibabel as nib
import numpy as np

new, old = Path(sys.argv[1]), Path(sys.argv[2])
report: dict = {}
ok = True
for name, atol in (("lesion_mask_mni.nii.gz", 0.0), ("ct_mni.nii.gz", 1e-4)):
    a, b = nib.load(str(new / name)), nib.load(str(old / name))
    da = np.asanyarray(a.dataobj).astype(np.float64)
    db = np.asanyarray(b.dataobj).astype(np.float64)
    same_grid = a.shape == b.shape and np.allclose(a.affine, b.affine, rtol=0, atol=1e-6)
    diff = float(np.max(np.abs(da - db))) if same_grid else float("inf")
    report[name] = {"same_grid": same_grid, "max_abs_difference": diff,
                    "identical_bytes": (new / name).read_bytes() == (old / name).read_bytes()}
    if name.startswith("lesion"):
        report[name]["voxels_new"] = int((da > 0).sum())
        report[name]["voxels_previous"] = int((db > 0).sum())
    ok = ok and same_grid and diff <= atol
report["equivalent"] = ok
print(json.dumps(report))
sys.exit(0 if ok else 1)
