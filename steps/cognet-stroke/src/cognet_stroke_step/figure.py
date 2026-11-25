"""Profile figure: an axial slice of the disconnection map with the lesion outline, and the
positive-disconnection score per concept as bars. matplotlib only (no nilearn in the image)."""

from __future__ import annotations

import numpy as np


def _axial(volume: np.ndarray, k: int) -> np.ndarray:
    # slice k of the working grid, rotated so that the second array axis (anterior) points up
    return np.rot90(volume[:, :, k])


def profile_figure(path, disconnection: np.ndarray, lesion: np.ndarray, profile: dict) -> None:
    """Write a PNG: left the axial slice through the lesion centre (disconnection map, voxels
    without a defined value in dark grey, lesion contour in black), right the
    positive-disconnection score per concept (filled bars: significant after correction)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    data = np.asarray(disconnection, dtype=np.float64)
    lesion = np.asarray(lesion, dtype=bool)
    idx = np.argwhere(lesion)
    k = int(np.round(idx[:, 2].mean())) if len(idx) else data.shape[2] // 2
    finite = data[np.isfinite(data)]
    vmax = float(np.max(np.abs(finite))) if finite.size else 0.0
    vmax = vmax if vmax > 0 else 1.0

    concepts = profile["concepts"]
    scores = [c["disconnection_positive"]["score"] for c in concepts]
    sig = [c["disconnection_positive"]["significant"] for c in concepts]
    plotted = [0.0 if s is None else s for s in scores]
    n = len(concepts)

    fig = plt.figure(figsize=(13, max(4.5, 0.28 * n + 1.5)), dpi=100)
    ax_map = fig.add_axes([0.02, 0.17, 0.40, 0.68])
    cmap = matplotlib.colormaps["coolwarm"].copy()
    cmap.set_bad("#4d4d4d")
    shown = np.ma.masked_invalid(_axial(data, k))
    image = ax_map.imshow(shown, cmap=cmap, vmin=-vmax, vmax=vmax, interpolation="nearest")
    outline = _axial(lesion.astype(float), k)
    if outline.any():
        ax_map.contour(outline, levels=[0.5], colors="k", linewidths=1.0)
    ax_map.set_title(
        "Disconnection map (axial), lesion outline in black,\nno defined value in dark grey",
        fontsize=10,
    )
    ax_map.axis("off")
    fig.colorbar(image, ax=ax_map, shrink=0.7)

    ax_bar = fig.add_axes([0.55, 0.17, 0.42, 0.68])
    y = np.arange(n)[::-1]
    colors = ["#1f77b4" if s else "#c7c7c7" for s in sig]
    ax_bar.barh(y, plotted, color=colors, edgecolor="#555555", linewidth=0.5)
    ax_bar.set_yticks(y)
    ax_bar.set_yticklabels([c["concept"] for c in concepts], fontsize=8)
    ax_bar.set_xlabel("positive disconnection score (CDR)")
    ax_bar.set_title("Concept profile (blue: significant after correction)")
    ax_bar.axvline(1.0, color="#999999", linewidth=0.6, linestyle=":")
    excluded = profile.get("excluded_parcels")
    footer = "Research use only - not a medical device"
    if excluded and excluded.get("count"):
        footer += (
            f"; {excluded['count']} parcels without a defined value were excluded"
            f" ({100 * excluded['fraction']:.1f} %)"
        )
    fig.text(0.5, 0.02, footer, ha="center", fontsize=9)
    fig.savefig(str(path), format="png", metadata={"Software": "cognet-stroke"})
    plt.close(fig)
