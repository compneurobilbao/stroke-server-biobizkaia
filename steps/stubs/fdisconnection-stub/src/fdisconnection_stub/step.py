"""STUB. Writes a deterministic synthetic map; it computes no functional disconnection.

Used only by CI and demo deployments to keep the pipeline wiring exercised until the real
step exists. The map is a smooth blob around the lesion centroid minus a smaller blob at
its mirror position, scaled per parcel of the connectome bundle's parcellation by factors
drawn from the run seed.
"""

from __future__ import annotations

import nibabel as nib
import numpy as np

from stroke_step_sdk import InputRejected, InternalFailure, StepContext

PARCELLATION = "parcellation/atlas.nii.gz"


def run(ctx: StepContext) -> None:
    ctx.warn(
        "stub_output",
        "Synthetic output of a stub step; not a functional disconnection analysis.",
    )
    img = nib.load(str(ctx.input_path("lesion_mask")))
    mask = np.asanyarray(img.dataobj) > 0
    if not mask.any():
        raise InputRejected(
            "mask_empty", "The lesion mask has no voxels set.", port="lesion_mask"
        )
    atlas_path = ctx.bundle_path("connectome") / PARCELLATION
    if not atlas_path.is_file():
        raise InternalFailure(
            "bundle_layout_unsupported",
            f"The connectome bundle has no {PARCELLATION}.",
        )
    labels = np.asanyarray(nib.load(str(atlas_path)).dataobj).astype(np.int64)
    if labels.shape != mask.shape:
        raise InternalFailure(
            "bundle_layout_unsupported", "The parcellation is not on the working grid."
        )

    rng = ctx.rng()
    factors = 0.75 + 0.5 * rng.random(int(labels.max()) + 1)
    factors[0] = 1.0  # background

    shape = mask.shape
    centroid = np.argwhere(mask).mean(axis=0)
    mirror = centroid.copy()
    mirror[0] = (shape[0] - 1) - centroid[0]
    ii, jj, kk = np.indices(shape)

    def blob(centre, sigma):
        d2 = (ii - centre[0]) ** 2 + (jj - centre[1]) ** 2 + (kk - centre[2]) ** 2
        return np.exp(-d2 / (2.0 * sigma**2))

    values = (blob(centroid, 14.0) - 0.3 * blob(mirror, 10.0)) * factors[labels]
    out = nib.Nifti1Image(values.astype(np.float32), img.affine)
    out.set_sform(img.affine, code=4)
    out.set_qform(img.affine, code=4)
    nib.save(out, str(ctx.output_path("fdisconnection_map")))
    ctx.add_software("nibabel", nib.__version__)
    ctx.add_software("numpy", np.__version__)
