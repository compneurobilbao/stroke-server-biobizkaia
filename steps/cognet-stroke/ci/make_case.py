"""Stage a container run of the cognet-stroke step on the reference case, with the
disconnection map altered (CI only; synthetic data).

    python steps/cognet-stroke/ci/make_case.py REFERENCE_DIR IN_DIR MODE

MODE: nan-parcels (every 7th parcel of the bundle's parcellation set to NaN, as flnm-fast
writes undefined parcels), inf (one infinite voxel), all-nan (no defined value).
Writes IN_DIR/lesion_mask.nii.gz, IN_DIR/fdisconnection_map.nii.gz and IN_DIR/params.json.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import nibabel as nib
import numpy as np

ref, in_dir, mode = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
in_dir.mkdir(parents=True, exist_ok=True)
shutil.copyfile(ref / "lesion_mask.nii.gz", in_dir / "lesion_mask.nii.gz")
img = nib.load(str(ref / "fdisconnection_map.nii.gz"))
data = np.asanyarray(img.dataobj).astype(np.float32)
labels = np.asanyarray(nib.load(str(ref / "bundle" / "parcellation.nii.gz")).dataobj)
if mode == "nan-parcels":
    ids = np.unique(labels[labels > 0])
    data[np.isin(labels, ids[::7])] = np.nan
    print(f"{ids[::7].size} of {ids.size} parcels set to NaN")
elif mode == "inf":
    data[40, 50, 40] = np.inf
elif mode == "all-nan":
    data[:] = np.nan
else:
    sys.exit(f"unknown mode {mode}")
nib.save(nib.Nifti1Image(data, img.affine), str(in_dir / "fdisconnection_map.nii.gz"))
(in_dir / "params.json").write_text('{"alpha": 0.05}\n', encoding="utf-8")
