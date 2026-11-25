"""Atlas space handling: bring the lesion into the space of the tract atlas.

The lesion mask arrives on the 2 mm MNI152NLin6Asym working grid. A tract atlas declares
the space of its streamline coordinates in ``metadata.space`` of its ``bundle.yaml``; an
atlas without that field is taken to be in MNI152NLin6Asym, which is what step 1.0.0
assumed for every atlas (so such atlases give the same results as before).

When the spaces differ, the lesion is resampled into the atlas space with a
``space-transform`` bundle before the streamlines are tested against it. Layout of that
bundle type (all at the bundle root):

``bundle.yaml``
    ``type: space-transform``; ``metadata.from_space`` and ``metadata.to_space`` name the
    two spaces (e.g. ``MNI152NLin6Asym`` and ``MNI152NLin2009aAsym``).
``coordinate_map.nii.gz``
    4-D float image of shape (X, Y, Z, 3) whose affine is a grid of the ``to_space``.
    Voxel (i, j, k) holds the RAS millimetre position in ``from_space`` that corresponds
    to the centre of that voxel. Resampling an image of ``from_space`` onto this grid is a
    lookup at those positions, so the map is the "pull-back" (image-resampling) direction
    of the spatial transform, the one ANTs and TemplateFlow call ``mode-image``.

Resampling of the binary lesion is nearest neighbour: every voxel of the target grid takes
the value of the lesion voxel nearest to its ``from_space`` position. Positions outside the
lesion grid, or not finite, are outside the lesion.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml

from stroke_step_sdk import InternalFailure

LESION_SPACE = "MNI152NLin6Asym"
DEFAULT_ATLAS_SPACE = LESION_SPACE
TRANSFORM_BUNDLE = "space_transform"
COORDINATE_MAP = "coordinate_map.nii.gz"
METHOD = "nearest neighbour"


def read_bundle_doc(bundle_dir: Path) -> dict:
    """The parsed ``bundle.yaml`` of a mounted bundle (the SDK has validated it)."""
    try:
        doc = yaml.safe_load((bundle_dir / "bundle.yaml").read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InternalFailure("bundle_invalid", "A bundle.yaml could not be read.") from exc
    if not isinstance(doc, dict):
        raise InternalFailure("bundle_invalid", "A bundle.yaml is not a mapping.")
    return doc


def _metadata(doc: dict) -> dict:
    meta = doc.get("metadata") or {}
    return meta if isinstance(meta, dict) else {}


def atlas_space(doc: dict) -> str:
    """Space of the atlas streamlines: ``metadata.space``, else MNI152NLin6Asym."""
    space = _metadata(doc).get("space", DEFAULT_ATLAS_SPACE)
    if not isinstance(space, str) or not space.strip():
        raise InternalFailure(
            "bundle_invalid", "The tract atlas declares an invalid metadata.space."
        )
    return space.strip()


@dataclass(frozen=True)
class Transform:
    bundle_id: str
    version: str
    from_space: str
    to_space: str
    coords: np.ndarray  # (X, Y, Z, 3) float32, RAS mm in from_space
    affine: np.ndarray  # 4 x 4, grid of to_space


def load_transform(bundle_dir: Path, from_space: str, to_space: str) -> Transform:
    """Read and check a space-transform bundle for the pair ``from_space`` -> ``to_space``."""
    doc = read_bundle_doc(bundle_dir)
    meta = _metadata(doc)
    pair = (meta.get("from_space"), meta.get("to_space"))
    if pair != (from_space, to_space):
        raise InternalFailure(
            "space_transform_mismatch",
            "The space-transform bundle does not map the lesion space to the atlas space.",
            details={
                "bundle_from_space": str(pair[0]),
                "bundle_to_space": str(pair[1]),
                "lesion_space": from_space,
                "atlas_space": to_space,
            },
        )
    path = bundle_dir / COORDINATE_MAP
    if not path.is_file():
        raise InternalFailure(
            "bundle_invalid", "The space-transform bundle has no coordinate_map.nii.gz."
        )
    try:
        img = nib.load(str(path))
        coords = np.asarray(img.get_fdata(dtype=np.float32))
    except Exception as exc:  # noqa: BLE001 - any reader error is a broken bundle
        raise InternalFailure(
            "bundle_invalid", "The coordinate map of the space-transform bundle is unreadable."
        ) from exc
    if coords.ndim == 5 and coords.shape[3] == 1:  # (X, Y, Z, 1, 3), the NIfTI vector layout
        coords = coords[:, :, :, 0, :]
    if coords.ndim != 4 or coords.shape[3] != 3 or min(coords.shape[:3]) < 1:
        raise InternalFailure(
            "bundle_invalid", "The coordinate map must have shape (X, Y, Z, 3)."
        )
    affine = np.asarray(img.affine, dtype=float)
    if not np.all(np.isfinite(affine)) or abs(np.linalg.det(affine[:3, :3])) < 1e-6:
        raise InternalFailure(
            "bundle_invalid", "The coordinate map has an invalid affine."
        )
    return Transform(
        bundle_id=str(doc.get("id")),
        version=str(doc.get("version")),
        from_space=from_space,
        to_space=to_space,
        coords=coords,
        affine=affine,
    )


def resample_nearest(
    mask: np.ndarray, mask_affine: np.ndarray, coords: np.ndarray, slab: int = 16
) -> np.ndarray:
    """Binary ``mask`` (on ``mask_affine``) looked up at the positions ``coords`` (X, Y, Z, 3,
    RAS mm) by nearest neighbour; returns a uint8 array of shape (X, Y, Z). Processed in
    slabs of ``slab`` planes to bound memory."""
    binary = np.asarray(mask) > 0
    inv = np.linalg.inv(np.asarray(mask_affine, dtype=float))
    rot, shift = inv[:3, :3].T, inv[:3, 3]
    upper = np.array(binary.shape[:3])
    out = np.zeros(coords.shape[:3], np.uint8)
    for k0 in range(0, coords.shape[2], slab):
        pts = coords[:, :, k0 : k0 + slab, :].reshape(-1, 3).astype(np.float64)
        vox = np.floor(pts @ rot + shift + 0.5)
        ok = np.all(np.isfinite(vox), axis=1)
        ok[ok] = np.all((vox[ok] >= 0) & (vox[ok] < upper), axis=1)
        idx = vox[ok].astype(np.int64)
        values = np.zeros(len(pts), np.uint8)
        values[ok] = binary[idx[:, 0], idx[:, 1], idx[:, 2]]
        out[:, :, k0 : k0 + slab] = values.reshape(coords.shape[0], coords.shape[1], -1)
    return out


def voxel_volume_mm3(affine: np.ndarray) -> float:
    return float(abs(np.linalg.det(np.asarray(affine, dtype=float)[:3, :3])))


@dataclass(frozen=True)
class Resampled:
    path: Path
    voxels_before: int
    volume_before_mm3: float
    voxels_after: int
    volume_after_mm3: float

    def summary(self) -> dict:
        return {
            "voxels_before": self.voxels_before,
            "volume_before_mm3": round(self.volume_before_mm3, 3),
            "voxels_after": self.voxels_after,
            "volume_after_mm3": round(self.volume_after_mm3, 3),
        }


def lesion_to_atlas_space(lesion: Path, transform: Transform, dest: Path) -> Resampled:
    """Resample the lesion file onto the transform's target grid and save it at ``dest``."""
    img = nib.load(str(lesion))
    data = np.asarray(img.dataobj) > 0
    out = resample_nearest(data, img.affine, transform.coords)
    target = nib.Nifti1Image(out, transform.affine)
    target.header.set_xyzt_units("mm")
    target.set_sform(transform.affine, code=2)  # aligned to a template
    target.set_qform(transform.affine, code=2)
    nib.save(target, str(dest))
    return Resampled(
        path=dest,
        voxels_before=int(data.sum()),
        volume_before_mm3=int(data.sum()) * voxel_volume_mm3(img.affine),
        voxels_after=int(out.sum()),
        volume_after_mm3=int(out.sum()) * voxel_volume_mm3(transform.affine),
    )
