"""Analytic head-like CT phantom, synthetic "patients" and MNI lesion masks.

The phantom is a closed-form function of MNI coordinates (millimetres), so no
public template image is needed or redistributed. A synthetic patient is the
phantom seen through a random affine plus a smooth sinusoidal deformation; the
lesion is an ellipsoid defined in MNI space, so its native-space mask and its
MNI-space ground truth are exact and consistent.
"""

from __future__ import annotations

import numpy as np

from .io import MNI2_AFFINE, MNI2_SHAPE

# Phantom geometry in MNI mm (centre, semi-axes).
HEAD_C = np.array([0.0, -17.0, 12.0])
HEAD_AX = np.array([80.0, 102.0, 84.0])
SKULL_AX = np.array([73.0, 95.0, 77.0])
BRAIN_AX = np.array([67.0, 88.0, 69.0])
WM_AX = np.array([48.0, 68.0, 48.0])
VENT_AX = np.array([6.0, 22.0, 11.0])
VENT_C = np.array([[-9.0, -12.0, 16.0], [9.0, -12.0, 16.0]])

HU_AIR, HU_SCALP, HU_BONE = -1000, 40, 900
HU_GREY, HU_WHITE, HU_CSF = 36, 28, 6
HU_LESION_DELTA = -14

# Native CT grid of the synthetic patients.
CT_SHAPE = (76, 92, 60)
CT_VOX = np.array([2.5, 2.5, 3.0])


def _r(points: np.ndarray, centre: np.ndarray, axes: np.ndarray) -> np.ndarray:
    """Normalised ellipsoidal radius of (N, 3) points."""
    return np.sqrt((((points - centre) / axes) ** 2).sum(axis=1))


def in_brain(points: np.ndarray) -> np.ndarray:
    return _r(points, HEAD_C, BRAIN_AX) <= 1.0


def ellipsoid(points: np.ndarray, centre, axes) -> np.ndarray:
    return _r(points, np.asarray(centre, float), np.asarray(axes, float)) <= 1.0


def phantom_hu(points: np.ndarray) -> np.ndarray:
    """Noise-free Hounsfield values of the phantom at MNI points (N, 3)."""
    hu = np.full(len(points), HU_AIR, dtype=np.float64)
    hu[_r(points, HEAD_C, HEAD_AX) <= 1] = HU_SCALP
    hu[_r(points, HEAD_C, SKULL_AX) <= 1] = HU_BONE
    brain = _r(points, HEAD_C, BRAIN_AX) <= 1
    hu[brain] = HU_GREY
    hu[_r(points, HEAD_C, WM_AX) <= 1] = HU_WHITE
    for c in VENT_C:
        hu[_r(points, c, VENT_AX) <= 1] = HU_CSF
    return hu


def grid_points(shape, affine) -> np.ndarray:
    """World coordinates of every voxel centre, C order of ``shape`` (N, 3)."""
    ijk = np.stack(
        np.meshgrid(*[np.arange(s) for s in shape], indexing="ij"), -1
    ).reshape(-1, 3)
    return ijk @ affine[:3, :3].T + affine[:3, 3]


def lesion_mask_on_mni2(lesion: dict) -> np.ndarray:
    """Binary mask (uint8) of a lesion spec on the 2 mm working grid, inside the brain."""
    pts = grid_points(MNI2_SHAPE, MNI2_AFFINE)
    m = np.zeros(len(pts), dtype=bool)
    for part in lesion["parts"]:
        m |= ellipsoid(pts, part["centre"], part["axes"])
    m &= in_brain(pts)
    return m.reshape(MNI2_SHAPE).astype(np.uint8)


def _rotation(angles_deg) -> np.ndarray:
    ax, ay, az = np.deg2rad(angles_deg)
    rx = np.array(
        [[1, 0, 0], [0, np.cos(ax), -np.sin(ax)], [0, np.sin(ax), np.cos(ax)]]
    )
    ry = np.array(
        [[np.cos(ay), 0, np.sin(ay)], [0, 1, 0], [-np.sin(ay), 0, np.cos(ay)]]
    )
    rz = np.array(
        [[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]]
    )
    return rz @ ry @ rx


def random_transform(rng: np.random.Generator) -> dict:
    """Random affine (MNI -> scanner) and smooth deformation parameters."""
    angles = rng.uniform(-8, 8, 3)
    scale = rng.uniform(0.95, 1.05, 3)
    shift = rng.uniform(-10, 10, 3)
    g = np.eye(4)
    g[:3, :3] = _rotation(angles) @ np.diag(scale)
    g[:3, 3] = shift
    modes = []
    for _ in range(3):
        direction = rng.normal(size=3)
        direction /= np.linalg.norm(direction)
        modes.append(
            {
                "amp": rng.uniform(-3, 3, 3),
                "k": direction / rng.uniform(120, 240),  # cycles per mm
                "phase": rng.uniform(0, 2 * np.pi),
            }
        )
    return {
        "angles_deg": angles,
        "scale": scale,
        "shift_mm": shift,
        "g": g,
        "modes": modes,
    }


def deformation(points: np.ndarray, modes: list[dict]) -> np.ndarray:
    d = np.zeros_like(points)
    for m in modes:
        d += np.sin(2 * np.pi * (points @ m["k"]) + m["phase"])[:, None] * m["amp"]
    return d


def native_affine(g: np.ndarray) -> np.ndarray:
    """Voxel -> scanner affine of the native CT, centring the head in the FOV."""
    n = np.diag(np.append(CT_VOX, 1.0))
    head_scanner = g[:3, :3] @ HEAD_C + g[:3, 3]
    n[:3, 3] = head_scanner - CT_VOX * (np.array(CT_SHAPE) - 1) / 2
    return n


def make_patient(rng: np.random.Generator, lesion: dict) -> dict:
    """Synthetic CT, native lesion mask and MNI ground truth for one lesion spec."""
    tr = random_transform(rng)
    n_aff = native_affine(tr["g"])
    scanner = grid_points(CT_SHAPE, n_aff)
    p0 = (scanner - tr["g"][:3, 3]) @ np.linalg.inv(tr["g"][:3, :3]).T
    mni = p0 + deformation(p0, tr["modes"])  # MNI coordinates of every native voxel

    hu = phantom_hu(mni)
    les = np.zeros(len(mni), dtype=bool)
    for part in lesion["parts"]:
        les |= ellipsoid(mni, part["centre"], part["axes"])
    les &= in_brain(mni)
    hu[les] += HU_LESION_DELTA
    hu += np.round(rng.normal(0.0, 2.0, len(hu)))
    ct = np.clip(hu, -1024, 3071).astype(np.int16).reshape(CT_SHAPE)
    return {
        "ct": ct,
        "mask": les.reshape(CT_SHAPE).astype(np.uint8),
        "affine": n_aff,
        "transform": tr,
        "mni_mask": lesion_mask_on_mni2(lesion),
    }
