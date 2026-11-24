"""Write an all-zero lesion mask on the 2 mm working grid (input for the mask_empty check).

    python step/ci/make_empty_mask.py CONTRACTS_DIR OUT.nii.gz
"""

from __future__ import annotations

import sys
from pathlib import Path

import nibabel as nib
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import make_reference  # noqa: E402

shape, affine = make_reference.working_grid(Path(sys.argv[1]))
nib.save(nib.Nifti1Image(np.zeros(shape, np.uint8), affine), sys.argv[2])
