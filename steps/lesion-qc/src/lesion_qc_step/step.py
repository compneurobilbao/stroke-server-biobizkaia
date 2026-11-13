"""Lesion intake and QC: validates a lesion mask and measures it.

Two input modes, chosen by which ports are present:

* ``lesion_mni``: a binary mask already in MNI152NLin6Asym on any voxel grid. It is checked,
  resampled to the working grid and written as ``lesion_mask_mni``.
* ``lesion_native`` (optional ``ct``): a binary mask on the CT voxel grid. QC runs on the
  native mask; nothing is resampled and no ``lesion_mask_mni`` is produced.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import matplotlib
import nibabel as nib
import numpy as np
import scipy
from scipy import ndimage

from stroke_step_sdk import InputRejected, QcFailed, StepContext, contracts

from .figure import render_figure

WORKING_GRID = "MNI152NLin6Asym-res2"
SPACE = "MNI152NLin6Asym"
SCHEMA_VERSION = "1.0"
EXTENT_MARGIN_MM = 4.0  # lesion voxel centres may exceed the grid box by this much
MIDLINE_MM = 1.0
AXIS_ALIGNED_MIN = 0.99  # dominant component of each normalised affine column
MAX_LISTED_COMPONENTS = 20
STRUCT_26 = np.ones((3, 3, 3), dtype=bool)


def _r(x, n=4):
    return round(float(x), n)


def _vec(a, n=4):
    return [_r(v, n) for v in a]


def _voxel_sizes(affine: np.ndarray) -> np.ndarray:
    return np.linalg.norm(affine[:3, :3], axis=0)


def _world(affine: np.ndarray, ijk: np.ndarray) -> np.ndarray:
    return ijk @ affine[:3, :3].T + affine[:3, 3]


def _load_3d(path: Path, port: str) -> tuple[nib.Nifti1Image, np.ndarray]:
    img = nib.load(str(path))
    shape = tuple(int(s) for s in img.shape)
    while len(shape) > 3 and shape[-1] == 1:
        shape = shape[:-1]
    if len(shape) != 3:
        raise InputRejected(
            "mask_not_3d",
            "The mask must be a 3D image (a single volume).",
            port=port,
            details={"n_dimensions": len(img.shape)},
        )
    data = np.asanyarray(img.dataobj).reshape(shape) != 0
    affine = np.asarray(img.affine, dtype=float)
    if not np.all(np.isfinite(affine)) or abs(np.linalg.det(affine[:3, :3])) < 1e-9:
        raise InputRejected(
            "affine_invalid",
            "The image header has a singular or non-finite voxel-to-world matrix.",
            port=port,
        )
    return nib.Nifti1Image(data.astype(np.uint8), affine), data


def _check_mni_space(src: nib.Nifti1Image, original, data: np.ndarray, grid: dict, ctx) -> None:
    """Declared-space checks for a user MNI mask."""
    port = "lesion_mni"
    hdr = original.header
    codes = (int(hdr["sform_code"]), int(hdr["qform_code"]))
    if max(codes) == 0:
        raise InputRejected(
            "space_not_declared",
            "The image header does not declare a coordinate system (sform and qform codes "
            "are both 0), so it cannot be confirmed to be in MNI152 space.",
            port=port,
        )
    if max(codes) == 1:
        raise InputRejected(
            "space_mismatch",
            "The image header declares scanner (native) coordinates, not MNI152 space. "
            "Use the CT mode for masks drawn on a CT.",
            port=port,
            details={"sform_code": codes[0], "qform_code": codes[1]},
        )
    if 4 not in codes:
        ctx.warn(
            "space_code_not_mni152",
            "The header declares an aligned or Talairach coordinate system rather than "
            "MNI152; the mask is assumed to be in MNI152NLin6Asym.",
            port=port,
        )
    affine = src.affine
    cols = affine[:3, :3] / _voxel_sizes(affine)
    if not np.all(np.abs(cols).max(axis=0) >= AXIS_ALIGNED_MIN):
        raise InputRejected(
            "space_mismatch",
            "The image axes are rotated (oblique) relative to the world axes, which is "
            "not the case for a mask in MNI152 space.",
            port=port,
        )
    idx = np.argwhere(data).astype(float)
    world = _world(affine, idx)
    g_aff = np.asarray(grid["affine"], dtype=float)
    gs = grid["shape"]
    corners = _world(
        g_aff,
        np.array(
            [[i, j, k] for i in (0, gs[0] - 1) for j in (0, gs[1] - 1) for k in (0, gs[2] - 1)],
            dtype=float,
        ),
    )
    lo = corners.min(axis=0) - EXTENT_MARGIN_MM
    hi = corners.max(axis=0) + EXTENT_MARGIN_MM
    outside = int(np.count_nonzero(np.any((world < lo) | (world > hi), axis=1)))
    if outside:
        raise InputRejected(
            "space_mismatch",
            "Part of the lesion lies outside the extent of the MNI152 template, so the "
            "mask is probably not in MNI152 space.",
            port=port,
            details={"voxels_outside": outside, "voxels_total": int(len(idx))},
        )


def _resample_to_grid(img: nib.Nifti1Image, grid: dict) -> np.ndarray:
    g_aff = np.asarray(grid["affine"], dtype=float)
    m = np.linalg.inv(img.affine) @ g_aff
    out = ndimage.affine_transform(
        np.asanyarray(img.dataobj).astype(np.float32),
        m[:3, :3],
        offset=m[:3, 3],
        output_shape=tuple(grid["shape"]),
        order=1,
        mode="constant",
        cval=0.0,
    )
    return out >= 0.5


def _same_grid(a_shape, a_aff, b_shape, b_aff, tol=1e-4) -> bool:
    return tuple(a_shape) == tuple(b_shape) and bool(
        np.allclose(a_aff, b_aff, atol=tol, rtol=0)
    )


def _write_nifti_gz(path: Path, data: np.ndarray, affine: np.ndarray) -> None:
    img = nib.Nifti1Image(data.astype(np.uint8), affine)
    img.header.set_data_dtype(np.uint8)
    img.set_qform(affine, code=4)
    img.set_sform(affine, code=4)
    path.write_bytes(gzip.compress(img.to_bytes(), mtime=0))


def _hemisphere(data: np.ndarray, affine: np.ndarray, min_fraction: float) -> dict:
    x = _world(affine, np.argwhere(data).astype(float))[:, 0]
    left = int(np.count_nonzero(x < -MIDLINE_MM))
    right = int(np.count_nonzero(x > MIDLINE_MM))
    mid = int(len(x) - left - right)
    lateral = left + right
    if lateral == 0:
        label = "midline"
    elif min(left, right) / lateral >= min_fraction:
        label = "bilateral"
    else:
        label = "left" if left > right else "right"
    return {"label": label, "left_voxels": left, "right_voxels": right, "midline_voxels": mid}


def run(ctx: StepContext) -> None:
    has_mni = ctx.has_input("lesion_mni")
    has_native = ctx.has_input("lesion_native")
    has_ct = ctx.has_input("ct")
    if has_mni and (has_native or has_ct):
        raise InputRejected(
            "conflicting_inputs",
            "Provide either an MNI-space mask or a CT with a native mask, not both.",
        )
    if not (has_mni or has_native):
        raise InputRejected(
            "lesion_input_missing",
            "No lesion mask was provided: supply an MNI-space mask or a native mask.",
        )

    mode = "mni_mask" if has_mni else "ct_native"
    port = "lesion_mni" if has_mni else "lesion_native"
    p = ctx.params
    grid = contracts.registry()["grids"][WORKING_GRID]

    src_img, src_data = _load_3d(ctx.input_path(port), port)
    if not src_data.any():
        raise InputRejected("mask_empty", "The lesion mask has no voxels set.", port=port)

    ct_data = None
    ct_affine = None
    resampled = False
    if mode == "mni_mask":
        original = nib.load(str(ctx.input_path(port)))
        _check_mni_space(src_img, original, src_data, grid, ctx)
        qc_affine = np.asarray(grid["affine"], dtype=float)
        resampled = not _same_grid(src_img.shape, src_img.affine, grid["shape"], qc_affine)
        if resampled:
            qc = _resample_to_grid(src_img, grid)
            ctx.log.info("mask resampled to the working grid")
            if not qc.any():
                raise InputRejected(
                    "mask_empty_after_resampling",
                    "The lesion is too small to survive resampling to the 2 mm working grid.",
                    port=port,
                )
        else:
            qc = src_data
        space = SPACE
    else:
        qc, qc_affine, space = src_data, src_img.affine, "native"
        if has_ct:
            ct_img = nib.load(str(ctx.input_path("ct")))
            if not _same_grid(
                ct_img.shape[:3], ct_img.affine, src_img.shape, src_img.affine, 1e-3
            ):
                raise InputRejected(
                    "ct_grid_mismatch",
                    "The CT and the lesion mask do not share the same voxel grid.",
                    port="ct",
                )
            ct_data = np.asanyarray(ct_img.dataobj).reshape(src_img.shape).astype(np.float32)
            ct_affine = ct_img.affine

    vox_mm3 = float(abs(np.linalg.det(qc_affine[:3, :3])))
    n_vox = int(np.count_nonzero(qc))
    volume_ml = n_vox * vox_mm3 / 1000.0
    src_vox_mm3 = float(abs(np.linalg.det(src_img.affine[:3, :3])))
    volume_src_ml = int(np.count_nonzero(src_data)) * src_vox_mm3 / 1000.0

    if volume_ml > p["max_volume_ml"]:
        raise QcFailed(
            "lesion_too_large",
            "The lesion volume exceeds the maximum accepted by this step.",
            port=port,
            details={"volume_ml": _r(volume_ml, 3), "max_volume_ml": p["max_volume_ml"]},
        )

    labels, count = ndimage.label(qc, structure=STRUCT_26)
    sizes = np.bincount(labels.ravel())[1:]
    comp_ml = np.sort(sizes)[::-1] * vox_mm3 / 1000.0

    if mode == "mni_mask":
        hemi = _hemisphere(qc, qc_affine, p["bilateral_min_fraction"])
    else:
        hemi = {
            "label": "not_assessed",
            "left_voxels": None,
            "right_voxels": None,
            "midline_voxels": None,
        }
    if hemi["label"] == "bilateral" and p["reject_bilateral"]:
        raise QcFailed(
            "bilateral_lesion",
            "The lesion involves both hemispheres, which this configuration does not accept.",
            port=port,
            details={"left_voxels": hemi["left_voxels"], "right_voxels": hemi["right_voxels"]},
        )

    warnings: list[dict] = []

    def warn(code: str, message: str) -> None:
        ctx.warn(code, message, port=port)
        warnings.append({"code": code, "message": message})

    if volume_ml < p["warn_below_ml"]:
        warn("small_lesion", "The lesion volume is below the warning threshold.")
    if count > 1:
        warn(
            "multiple_components",
            f"The mask has {count} disconnected components (26-connectivity).",
        )
    if hemi["label"] == "bilateral":
        warn("bilateral_lesion", "The lesion involves both hemispheres.")
    if resampled and abs(volume_ml - volume_src_ml) > 0.1 * volume_src_ml:
        warn(
            "volume_changed_by_resampling",
            "Resampling to the working grid changed the lesion volume by more than 10 percent.",
        )
    ctx.log.info(
        "QC: %d voxels, %.3f ml, %d components, hemisphere %s",
        n_vox, volume_ml, count, hemi["label"],
    )

    idx = np.argwhere(qc)
    world = _world(qc_affine, idx.astype(float))
    result = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "input_space": space,
        "grid": {
            "name": WORKING_GRID if mode == "mni_mask" else None,
            "shape": [int(s) for s in qc.shape],
            "voxel_size_mm": _vec(_voxel_sizes(qc_affine)),
            "source_shape": [int(s) for s in src_img.shape],
            "source_voxel_size_mm": _vec(_voxel_sizes(src_img.affine)),
            "resampled": resampled,
        },
        "n_voxels": n_vox,
        "volume_ml": _r(volume_ml),
        "volume_source_ml": _r(volume_src_ml),
        "hemisphere": hemi,
        "components": {
            "count": int(count),
            "connectivity": 26,
            "volumes_ml": [_r(v) for v in comp_ml[:MAX_LISTED_COMPONENTS]],
            "largest_fraction": _r(sizes.max() / sizes.sum()),
        },
        "bounding_box": {
            "voxel": {
                "min": [int(v) for v in idx.min(axis=0)],
                "max": [int(v) for v in idx.max(axis=0)],
            },
            "world_mm": {
                "min": _vec(world.min(axis=0), 2),
                "max": _vec(world.max(axis=0), 2),
            },
        },
        "centroid_mm": _vec(world.mean(axis=0), 2),
        "warnings": warnings,
        "thresholds": {
            "warn_below_ml": p["warn_below_ml"],
            "max_volume_ml": p["max_volume_ml"],
            "reject_bilateral": p["reject_bilateral"],
            "bilateral_min_fraction": p["bilateral_min_fraction"],
        },
    }

    render_figure(ctx.output_path("qc_figure"), qc, qc_affine, result, ct_data, ct_affine)
    ctx.output_path("lesion_qc").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if mode == "mni_mask":
        _write_nifti_gz(ctx.output_path("lesion_mask_mni"), qc, qc_affine)

    ctx.add_software("nibabel", nib.__version__)
    ctx.add_software("numpy", np.__version__)
    ctx.add_software("scipy", scipy.__version__)
    ctx.add_software("matplotlib", matplotlib.__version__)
