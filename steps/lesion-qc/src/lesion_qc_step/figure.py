"""QC figure: orthogonal views of the lesion, rendered offline with matplotlib (Agg)."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import nibabel as nib  # noqa: E402
import numpy as np  # noqa: E402


def _canonical(data: np.ndarray, affine: np.ndarray):
    img = nib.as_closest_canonical(nib.Nifti1Image(data, affine))
    return np.asanyarray(img.dataobj), img.affine


def render_figure(path: Path, mask, affine, result: dict, ct=None, ct_affine=None) -> None:
    m, aff = _canonical(mask.astype(np.uint8), affine)
    bg = None
    if ct is not None:
        bg, _ = _canonical(ct, ct_affine)
    zooms = np.linalg.norm(aff[:3, :3], axis=0)
    centre = np.round(np.argwhere(m).mean(axis=0)).astype(int)
    origin = np.linalg.inv(aff) @ np.array([0, 0, 0, 1.0])
    mni = result["mode"] == "mni_mask"

    fig = plt.figure(figsize=(11, 7.2), dpi=100)
    views = [("Axial", 2, (0, 1)), ("Coronal", 1, (0, 2)), ("Sagittal", 0, (1, 2))]
    for n, (title, drop, (a, b)) in enumerate(views):
        ax = fig.add_subplot(2, 3, n + 1)
        suffix = " (projection)" if bg is None else f" (slice {centre[drop]})"
        ax.set_title(title + suffix, fontsize=10)
        aspect = zooms[b] / zooms[a]
        sl = [slice(None)] * 3
        if bg is None:
            proj = m.max(axis=drop).astype(float)
            ax.imshow(proj.T, origin="lower", cmap="Reds", vmin=0, vmax=1,
                      aspect=aspect, interpolation="nearest")
        else:
            sl[drop] = centre[drop]
            ax.imshow(bg[tuple(sl)].T, origin="lower", cmap="gray", vmin=0, vmax=80,
                      aspect=aspect, interpolation="nearest")
            lesion = np.ma.masked_equal(m[tuple(sl)].T, 0)
            ax.imshow(lesion, origin="lower", cmap="autumn", alpha=0.6, vmin=0, vmax=1,
                      aspect=aspect, interpolation="nearest")
        ax.set_xlim(-0.5, m.shape[a] - 0.5)
        ax.set_ylim(-0.5, m.shape[b] - 0.5)
        if mni:
            ax.axvline(origin[a], color="0.5", lw=0.6, ls="--")
            ax.axhline(origin[b], color="0.5", lw=0.6, ls="--")
        ax.set_xticks([])
        ax.set_yticks([])
        if a == 0:
            ax.text(0.02, 0.5, "L", transform=ax.transAxes, color="c", fontsize=11)
            ax.text(0.95, 0.5, "R", transform=ax.transAxes, color="c", fontsize=11)
    ax = fig.add_subplot(2, 1, 2)
    ax.axis("off")
    h, c, g, bb = result["hemisphere"], result["components"], result["grid"], result["bounding_box"]
    side = ""
    if h["left_voxels"] is not None:
        side = (f" (left {h['left_voxels']}, right {h['right_voxels']}, "
                f"midline {h['midline_voxels']} voxels)")
    lines = [
        f"Mode: {result['mode']}    space: {result['input_space']}",
        f"Volume: {result['volume_ml']:.3f} mL ({result['n_voxels']} voxels)",
        f"Hemisphere: {h['label']}{side}",
        f"Components: {c['count']} (largest {c['largest_fraction'] * 100:.1f} % of volume)",
        f"Grid: {g['name'] or 'native'}  shape {g['shape']}  voxel {g['voxel_size_mm']} mm"
        + ("  (resampled)" if g["resampled"] else ""),
        f"Bounding box (mm): {bb['world_mm']['min']} to {bb['world_mm']['max']}",
        "Warnings: " + (", ".join(w["code"] for w in result["warnings"]) or "none"),
    ]
    ax.text(0.01, 0.95, "\n".join(lines), va="top", ha="left", family="monospace", fontsize=10)
    fig.tight_layout()
    fig.savefig(path, format="png", metadata={"Software": None})
    plt.close(fig)
