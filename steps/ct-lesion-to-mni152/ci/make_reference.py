"""Build the reference case from a REAL head CT template and a known transform.

Source image: the public High Resolution CT Template (Muschelli 2020, built from CQ500 scans,
CC BY-NC-SA 4.0), downloaded by CI at a pinned commit with SHA-256 checks
(step/ci/fetch_reference_data.sh). Nothing from it is committed.

Steps (fixed seed, deterministic):
 1. Warp the template (with skull) into the MNI152 1 mm grid with the displacement field
    distributed with it. This is only the source image; the correlation with the package's own
    MNI-space template is printed as a sanity check.
 2. Insert a hypodense ellipsoidal lesion (defined in MNI space, inside the warped brain).
 3. Simulate a "native" CT: random affine (rotation up to 8 deg, scale +-5 %, shift +-10 mm) plus
    a smooth sinusoidal deformation (generator code of the stroke-server fixtures at the pinned
    commit), sampled on a 1 mm native grid, with Gaussian noise.
 4. Ground truth: the MNI lesion on the 2 mm working grid, and the native lesion mask, both
    exact for the simulation.

    python step/ci/make_reference.py DATA_DIR FIXTURES_ROOT OUT_DIR
Output: ct.nii.gz, lesion_mask_native.nii.gz, lesion_mask_mni.nii.gz, source_info.json
"""

import json
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
from scipy import ndimage

data_dir, fixtures_root, out = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
sys.path.insert(0, str(fixtures_root))
from fixtures import phantom  # noqa: E402
from fixtures.io import MNI2_AFFINE, MNI2_SHAPE, write_nifti_gz  # noqa: E402

SEED = 20261113
LESION = {"centre": [-38.0, -15.0, 15.0], "axes": [16.0, 13.0, 14.0]}  # MNI mm, as fixture case-01
LESION_DELTA_HU = -14.0
NOISE_HU = 2.0
NATIVE_SHAPE = (200, 240, 200)
NATIVE_VOX = np.array([1.0, 1.0, 1.0])


def warp_to_mni(template_path: Path, warp: nib.Nifti1Image) -> np.ndarray:
    """Template -> MNI 1 mm grid. The field gives, for each MNI voxel, the displacement (ITK
    LPS convention, so x and y change sign in RAS) to the template's physical point."""
    img = nib.load(str(template_path))
    u = np.asanyarray(warp.dataobj)[:, :, :, 0, :].reshape(-1, 3) * np.array([-1.0, -1.0, 1.0])
    idx = np.indices(warp.shape[:3]).reshape(3, -1).T
    q = nib.affines.apply_affine(warp.affine, idx) + u
    vox = nib.affines.apply_affine(np.linalg.inv(img.affine), q).T.reshape(3, *warp.shape[:3])
    return ndimage.map_coordinates(
        np.asanyarray(img.dataobj).astype(np.float32), vox, order=1, mode="constant", cval=0.0
    ).astype(np.float32)


def main() -> None:
    warp = nib.load(str(data_dir / "Template_to_MNIcomptx.nii.gz"))
    mni1_affine = warp.affine  # the 1 mm MNI152 grid
    ct_mni = warp_to_mni(data_dir / "template_with_skull_1mm.nii.gz", warp)
    brain_mni = warp_to_mni(data_dir / "template_1mm.nii.gz", warp) > 0

    # Lesion in MNI space, inside the brain
    pts1 = phantom.grid_points(warp.shape[:3], mni1_affine)
    les1 = phantom.ellipsoid(pts1, LESION["centre"], LESION["axes"]).reshape(warp.shape[:3]) & brain_mni
    ct_les = ct_mni + LESION_DELTA_HU * les1

    # Ground truth on the 2 mm working grid (evaluated at 2 mm voxel centres)
    pts2 = phantom.grid_points(MNI2_SHAPE, MNI2_AFFINE)
    inv1 = np.linalg.inv(mni1_affine)
    v2 = nib.affines.apply_affine(inv1, pts2)
    brain2 = ndimage.map_coordinates(brain_mni.astype(np.float32), v2.T, order=0, mode="constant") > 0
    truth = (phantom.ellipsoid(pts2, LESION["centre"], LESION["axes"]) & brain2).reshape(MNI2_SHAPE)

    # Native simulation
    phantom.CT_SHAPE = NATIVE_SHAPE
    phantom.CT_VOX = NATIVE_VOX
    rng = np.random.default_rng(SEED)
    tr = phantom.random_transform(rng)
    n_aff = phantom.native_affine(tr["g"])
    scanner = phantom.grid_points(NATIVE_SHAPE, n_aff)
    p0 = (scanner - tr["g"][:3, 3]) @ np.linalg.inv(tr["g"][:3, :3]).T
    mni = p0 + phantom.deformation(p0, tr["modes"])
    vox = nib.affines.apply_affine(inv1, mni).T
    ct_nat = ndimage.map_coordinates(ct_les, vox, order=1, mode="constant", cval=0.0)
    brain_nat = ndimage.map_coordinates(brain_mni.astype(np.float32), vox, order=0, mode="constant") > 0
    les_nat = phantom.ellipsoid(mni, LESION["centre"], LESION["axes"]) & brain_nat
    ct_nat = ct_nat + np.round(rng.normal(0.0, NOISE_HU, ct_nat.shape))
    ct_nat = np.clip(ct_nat, -1024, 3071).astype(np.int16).reshape(NATIVE_SHAPE)

    out.mkdir(parents=True, exist_ok=True)
    write_nifti_gz(out / "ct.nii.gz", ct_nat, n_aff, mni=False)
    write_nifti_gz(out / "lesion_mask_native.nii.gz", les_nat.reshape(NATIVE_SHAPE).astype(np.uint8), n_aff, mni=False)
    write_nifti_gz(out / "lesion_mask_mni.nii.gz", truth.astype(np.uint8), MNI2_AFFINE, mni=True)

    info = {
        "seed": SEED,
        "rotation_deg": [round(float(x), 3) for x in tr["angles_deg"]],
        "scale": [round(float(x), 4) for x in tr["scale"]],
        "shift_mm": [round(float(x), 3) for x in tr["shift_mm"]],
        "lesion_voxels_native": int(les_nat.sum()),
        "lesion_voxels_mni_2mm": int(truth.sum()),
        "voxels_above_100hu": int((ct_nat > 100).sum()),
    }
    ref = data_dir / "ct_in_MNI152.nii.gz"
    if ref.exists():  # optional sanity check against the package template
        m = np.asanyarray(nib.load(str(ref)).dataobj).astype(np.float32)
        sel = m > 0
        info["corr_warped_template_vs_package_template"] = round(float(np.corrcoef(ct_mni[sel], m[sel])[0, 1]), 4)
    (out / "source_info.json").write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(info))


if __name__ == "__main__":
    main()
