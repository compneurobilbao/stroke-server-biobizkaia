"""The step's own logic. The SDK handles everything else in the contract."""

from __future__ import annotations

import json

import nibabel as nib
import numpy as np

from stroke_step_sdk import InputRejected, QcFailed, StepContext


def run(ctx: StepContext) -> None:
    """Measure the lesion mask and write ``lesion_qc.json``.

    By the time this runs, the SDK has validated the parameters and checked that
    ``lesion_mask`` exists, is a gzip NIfTI on the working grid and is binary.
    """
    img = nib.load(str(ctx.input_path("lesion_mask")))
    voxels = int(np.count_nonzero(np.asanyarray(img.dataobj)))
    if voxels == 0:
        raise InputRejected(
            "mask_empty", "The lesion mask has no voxels set.", port="lesion_mask"
        )

    voxel_mm3 = float(abs(np.linalg.det(img.affine[:3, :3])))
    volume_ml = voxels * voxel_mm3 / 1000.0
    ctx.log.info("lesion has %d voxels, %.3f ml", voxels, volume_ml)

    max_ml = ctx.params["max_volume_ml"]
    if volume_ml > max_ml:
        raise QcFailed(
            "lesion_too_large",
            "The lesion volume exceeds the maximum accepted by this step.",
            port="lesion_mask",
            details={"volume_ml": round(volume_ml, 3), "max_volume_ml": max_ml},
        )
    if volume_ml < ctx.params["warn_below_ml"]:
        ctx.warn(
            "small_lesion",
            "The lesion volume is below the warning threshold.",
            port="lesion_mask",
        )

    result = {
        "n_voxels": voxels,
        "voxel_volume_mm3": round(voxel_mm3, 6),
        "volume_ml": round(volume_ml, 6),
    }
    ctx.output_path("lesion_qc").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    ctx.add_software("nibabel", nib.__version__)
    ctx.add_software("numpy", np.__version__)
