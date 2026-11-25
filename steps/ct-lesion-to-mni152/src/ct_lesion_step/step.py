"""Contract wrapper around the unchanged CTLesion2MNI152 workflow.

Flow: check inputs -> run ``registration_workflow`` in scratch (FSL + elastix, 1 mm template
grid) -> binarise the 1 mm mask -> optional ventricle removal (the package's own
``ventricle_removal`` CLI on a throw-away BIDS-like tree) -> reduce to the 2 mm working grid
-> write outputs and the QC figure.
"""

from __future__ import annotations

import importlib.metadata as md
import re
import subprocess
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml
from scipy import ndimage

from stroke_step_sdk import (
    InputRejected,
    InternalFailure,
    QcFailed,
    StepContext,
    contracts,
)

BAND_RE = re.compile(r"^([0-9]{1,3})-([0-9]{1,3})$")
MIN_LESION_IN_BRAIN = 0.5  # fraction of the lesion that must lie in the template brain
GRID_SNAP_MM = 1e-3  # tolerance for snapping tool-written affines to the registry grid
SKULL_MSG = "No skull cluster above the threshold was found; the image may not be a head CT."


def _grids() -> dict:
    registry = yaml.safe_load(
        (contracts.contracts_dir() / "artefact-types.yaml").read_text(encoding="utf-8")
    )
    return {
        k: (tuple(v["shape"]), np.array(v["affine"], dtype=float))
        for k, v in registry["grids"].items()
    }


def age_from_band(band: str) -> int:
    """Midpoint (whole years) of a ``"lo-hi"`` band."""
    m = BAND_RE.match(band)
    lo, hi = (int(m.group(1)), int(m.group(2))) if m else (1, 0)
    if lo > hi or hi > 120:
        raise InputRejected(
            "invalid_parameters", "The age band is not a valid range of years."
        )
    return (lo + hi) // 2


def check_inputs(ct_path: Path, mask_path: Path) -> None:
    ct = nib.load(str(ct_path))
    mask = nib.load(str(mask_path))
    if len(ct.shape) != 3:
        raise InputRejected("ct_not_3d", "The CT image is not a 3-D volume.", port="ct")
    if mask.shape != ct.shape or not np.allclose(
        mask.affine, ct.affine, rtol=0, atol=1e-3
    ):
        raise InputRejected(
            "mask_grid_mismatch",
            "The lesion mask does not share the voxel grid of the CT.",
            port="lesion_mask",
        )
    if not np.any(np.asanyarray(mask.dataobj)):
        raise InputRejected(
            "mask_empty", "The lesion mask has no voxels set.", port="lesion_mask"
        )


def _skull_missing(work: Path) -> bool:
    """True if the workflow's skull-cluster step found no cluster (all sizes zero)."""
    sizes = work / "ct_skull_cluster_sizes.nii.gz"
    if not sizes.exists():
        return False
    return float(np.max(np.asanyarray(nib.load(str(sizes)).dataobj))) <= 0


def run_workflow(ct_path: Path, mask_path: Path, work: Path) -> None:
    """Run the package's CLI entry point exactly as the console script would."""
    from ct_lesion_to_mni152 import registration_workflow as wf

    argv = sys.argv
    sys.argv = ["ct_lesion_to_mni152", "-p", str(ct_path), "-o", str(work), "-l", str(mask_path)]
    # The package runs its FSL commands with subprocess.call and ignores their exit codes.
    # Inside the step only, swap in check_call so that a failing command stops the run;
    # the package itself is left unchanged.
    package_call = wf.call
    wf.call = subprocess.check_call
    try:
        wf.register_ct_and_lesion_to_mni152()
    except subprocess.CalledProcessError as exc:
        if _skull_missing(work):
            raise QcFailed("skull_not_found", SKULL_MSG, port="ct") from exc
        cmd = Path(str(exc.cmd[0])).name if exc.cmd else "unknown"
        raise InternalFailure(
            "fsl_command_failed",
            "An FSL command of the registration workflow failed.",
            details={"command": cmd, "exit_code": int(exc.returncode)},
        ) from exc
    except Exception as exc:
        # fsl-cluster exits 0 when no cluster is big enough; the workflow then continues with an
        # empty skull image and fails later (typically inside elastix). Name the real cause.
        if _skull_missing(work):
            raise QcFailed("skull_not_found", SKULL_MSG, port="ct") from exc
        raise
    finally:
        wf.call = package_call
        sys.argv = argv
    if _skull_missing(work):
        raise QcFailed("skull_not_found", SKULL_MSG, port="ct")


def ventricle_removal(mask_1mm: nib.Nifti1Image, age: int, work: Path) -> np.ndarray:
    """Run the package's ventricle_removal CLI on a minimal project tree; return the result."""
    from ventricle_removal import v_removal_workflow as vr

    project = work / "vr_project"
    (project / "sub-1" / "ct").mkdir(parents=True)
    (project / "participants.tsv").write_text(f"BIDS\tAGE\nsub-1\t{age}\n", encoding="utf-8")
    nib.save(mask_1mm, str(project / "sub-1" / "ct" / "sub-1_ct_lesion_mask_MNI152.nii.gz"))
    argv = sys.argv
    sys.argv = ["ventricle_removal", "-p", str(project), "-s", "sub-1"]
    try:
        vr.main()
    finally:
        sys.argv = argv
    out = project / "sub-1" / "ct" / "sub-1_ct_lesion_mask_MNI152_novent.nii.gz"
    return np.asanyarray(nib.load(str(out)).dataobj) > 0


def to_working_grid(mask_1mm: np.ndarray, threshold: float) -> np.ndarray:
    """Reduce a binary mask on the 1 mm grid to the aligned 2 mm grid.

    2 mm voxel i covers 1 mm voxels 2i-1..2i+1 (edge voxels counted by half), so the
    separable kernel [.25, .5, .25] sampled at even indices is the covered volume fraction.
    """
    f = mask_1mm.astype(np.float32)
    for axis in range(3):
        f = ndimage.convolve1d(f, [0.25, 0.5, 0.25], axis=axis, mode="constant")
    return f[::2, ::2, ::2] >= threshold


def load_on_grid(path: Path, grid, what: str, ctx: StepContext, order: int) -> np.ndarray:
    """Load a tool-written image and make sure it sits on ``grid`` (resample if not)."""
    shape, affine = grid
    img = nib.load(str(path))
    if img.shape == shape and np.allclose(img.affine, affine, rtol=0, atol=GRID_SNAP_MM):
        return np.asanyarray(img.dataobj).astype(np.float32)
    from nibabel.processing import resample_from_to

    ctx.warn(
        "resampled_to_template_grid",
        f"The {what} written by the workflow was not on the expected MNI152 grid and was resampled.",
    )
    out = resample_from_to(img, (shape, affine), order=order, mode="constant", cval=0)
    return np.asanyarray(out.dataobj).astype(np.float32)


def qc_figure(path: Path, ct_mni: np.ndarray, brain: np.ndarray, lesion: np.ndarray) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    idx = np.argwhere(lesion)
    centre = np.round(idx.mean(axis=0)).astype(int) if len(idx) else np.array(ct_mni.shape) // 2
    fig, axes = plt.subplots(1, 3, figsize=(12, 4.4), dpi=100)
    for ax, (title, axis) in zip(axes, [("axial", 2), ("coronal", 1), ("sagittal", 0)]):
        sl = [slice(None)] * 3
        sl[axis] = int(centre[axis])
        sl = tuple(sl)
        ax.imshow(np.rot90(ct_mni[sl]), cmap="gray", vmin=0, vmax=100, interpolation="nearest")
        b = np.rot90(brain[sl] > 0).astype(float)
        if b.any():
            ax.contour(b, levels=[0.5], colors="cyan", linewidths=0.8)
        les = np.rot90(lesion[sl]).astype(float)
        if les.any():
            ax.contour(les, levels=[0.5], colors="red", linewidths=1.2)
        ax.set_title(title)
        ax.axis("off")
    fig.suptitle("Registered CT (grey), template brain outline (cyan), lesion (red)")
    fig.tight_layout()
    fig.savefig(str(path), format="png", metadata={"Software": "ct-lesion-to-mni152"})
    plt.close(fig)


def run(ctx: StepContext) -> None:
    ct_path = ctx.input_path("ct")
    mask_path = ctx.input_path("lesion_mask")
    params = ctx.params
    age = age_from_band(params["age_band"]) if params["age_band"] else None
    check_inputs(ct_path, mask_path)

    grids = _grids()
    grid1, grid2 = grids["MNI152NLin6Asym-res1"], grids["MNI152NLin6Asym-res2"]

    work = ctx.tmp_dir / "work"
    work.mkdir()
    ctx.log.info("running the CTLesion2MNI152 registration workflow")
    run_workflow(ct_path, mask_path, work)

    ct_arr = load_on_grid(work / "ct_MNI152.nii.gz", grid1, "registered CT", ctx, 1)
    mask_f = load_on_grid(work / "lesion_mask_MNI152.nii.gz", grid1, "registered mask", ctx, 0)
    mask_1mm = mask_f >= 0.5
    pre = work / "lesion_mask_pre_mni.nii.gz"  # after the affine stage, before the B-spline
    if pre.exists():
        n_pre = int((np.asanyarray(nib.load(str(pre)).dataobj) > 0.5).sum())
        n_in = int(np.count_nonzero(np.asanyarray(nib.load(str(mask_path)).dataobj)))
        ctx.log.info("lesion voxels: %d native input, %d after the affine stage", n_in, n_pre)
    ctx.log.info(
        "workflow mask on the 1 mm grid: max %.3f, %d voxels > 0, %d voxels >= 0.5",
        float(mask_f.max()), int((mask_f > 0).sum()), int(mask_1mm.sum()),
    )

    from ct_lesion_to_mni152 import data as ctdata

    brain = np.asanyarray(nib.load(ctdata.MNI152_BRAIN_PATH).dataobj)
    n_total = int(mask_1mm.sum())
    if n_total == 0:
        raise QcFailed("lesion_lost", "The lesion mask is empty after registration.", port="lesion_mask")
    inside = int((mask_1mm & (brain > 0)).sum()) / n_total
    ctx.log.info("fraction of the registered lesion inside the template brain: %.3f", inside)
    if inside < MIN_LESION_IN_BRAIN:
        raise QcFailed(
            "lesion_outside_brain",
            "After registration, most of the lesion lies outside the template brain.",
            port="lesion_mask",
            details={"fraction_inside": round(inside, 3), "minimum": MIN_LESION_IN_BRAIN},
        )

    if age is not None:
        ctx.log.info("removing the ventricle template of the age band containing %d years", age)
        img = nib.Nifti1Image(mask_1mm.astype(np.int16), grid1[1])
        mask_1mm = ventricle_removal(img, age, work)

    final = to_working_grid(mask_1mm, params["mask_threshold"])
    if not final.any():
        raise QcFailed(
            "lesion_lost",
            "The lesion mask is empty after ventricle removal or reduction to the working grid.",
            port="lesion_mask",
        )
    ctx.log.info("lesion: %d voxels on the 1 mm grid, %d on the 2 mm grid", mask_1mm.sum(), final.sum())

    nib.save(nib.Nifti1Image(final.astype(np.uint8), grid2[1]), str(ctx.output_path("lesion_mask_mni")))
    nib.save(nib.Nifti1Image(ct_arr.astype(np.float32), grid1[1]), str(ctx.output_path("ct_mni")))
    qc_figure(ctx.output_path("qc_figure"), ct_arr, brain, mask_1mm)

    for name in ("itk", "itk-elastix", "nibabel", "numpy", "scipy"):
        ctx.add_software(name, md.version(name))
    ctx.add_software("FSL", "6.0.7.6")
