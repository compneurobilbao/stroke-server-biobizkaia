"""Figures of the report, rendered offline (Agg backend, nilearn's bundled template).

Figures contain no text in any natural language beyond tract names and numbers: captions
and axis meanings are written by the language templates.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import nibabel as nib  # noqa: E402
import numpy as np  # noqa: E402

DPI = 130
MAX_BARS = 20
CT_WINDOW = (0.0, 80.0)  # HU brain window for the optional CT background


def _background(ct_mni: Path | None):
    """The CT resampled by the normalisation if present, else nilearn's bundled template."""
    if ct_mni is not None:
        img = nib.load(str(ct_mni))
        data = np.clip(np.asanyarray(img.dataobj).astype(np.float32), *CT_WINDOW)
        return nib.Nifti1Image(data, img.affine), "ct"
    from nilearn.datasets import load_mni152_template

    return load_mni152_template(resolution=2), "template"


def _cut_coords(mask_path: Path) -> tuple[float, float, float]:
    img = nib.load(str(mask_path))
    idx = np.argwhere(np.asanyarray(img.dataobj) > 0)
    xyz = nib.affines.apply_affine(img.affine, idx.mean(axis=0))
    return tuple(float(round(v, 1)) for v in xyz)


def lesion_figure(mask_path: Path, out: Path, ct_mni: Path | None = None) -> str:
    """Orthogonal views of the lesion through its centroid. Returns the background kind."""
    from nilearn import plotting

    bg, kind = _background(ct_mni)
    display = plotting.plot_roi(
        nib.load(str(mask_path)),
        bg_img=bg,
        cut_coords=_cut_coords(mask_path),
        display_mode="ortho",
        draw_cross=False,
        cmap="autumn",
        alpha=0.85,
        black_bg=False,
        colorbar=False,
    )
    display.savefig(str(out), dpi=DPI)
    display.close()
    return kind


def disconnection_map_figure(
    map_path: Path, mask_path: Path, out: Path, ct_mni: Path | None = None
) -> bool:
    """Functional disconnection map (larger = more disconnection). False if the map is empty."""
    from nilearn import plotting

    img = nib.load(str(map_path))
    vmax = float(np.asanyarray(img.dataobj).max())
    if vmax <= 0:
        return False
    bg, _ = _background(ct_mni)
    display = plotting.plot_stat_map(
        img,
        bg_img=bg,
        cut_coords=_cut_coords(mask_path),
        display_mode="ortho",
        draw_cross=False,
        cmap="YlOrRd",
        symmetric_cbar=False,
        vmin=0.0,
        vmax=vmax,
        threshold=vmax * 0.01,
        black_bg=False,
        colorbar=True,
    )
    display.savefig(str(out), dpi=DPI)
    display.close()
    return True


def tract_figure(rows: list[dict], out: Path) -> int:
    """Horizontal bars of the most disconnected tracts. Returns the number of bars."""
    top = rows[:MAX_BARS]
    height = 0.9 + 0.32 * len(top)
    fig, ax = plt.subplots(figsize=(7.2, height), dpi=DPI)
    pos = np.arange(len(top))[::-1]
    ax.barh(pos, [r["disconnection"] for r in top], color="#b5452f", height=0.7)
    ax.set_yticks(pos)
    ax.set_yticklabels([r["tract"] for r in top], fontsize=8)
    ax.set_xlim(0, 1)
    ax.tick_params(axis="x", labelsize=8)
    ax.grid(axis="x", color="0.85", lw=0.6)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    fig.tight_layout()
    fig.savefig(str(out), format="png", metadata={"Software": None})
    plt.close(fig)
    return len(top)
