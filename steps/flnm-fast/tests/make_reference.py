"""Synthetic reference dataset of the flnm-fast step (nothing is committed).

Writes, deterministically (fixed formulas, no random generator, no time stamps):

* ``reference/bundle/``: a synthetic ``normative-fc`` bundle (bundle.yaml,
  ``fc_row_subject_col.npy``, ``normative_subjects.csv``, ``standard/<parcellation>.nii.gz``)
  with 27 block parcels, 2100 participants with sex and age, and connectivity values from an
  integer hash;
* ``reference/lesion_mask.nii.gz``: a ball straddling several parcels on the 2 mm working grid;
* ``reference/expected/fdisconnection_map.nii.gz``: the expected z-map, computed here with
  the standard library and numpy only (exact Student t and normal quantile functions), not
  by running the engine or the step.

Nothing is real data. Used by the step tests, by ``stroke-step-reftest`` (contract 1.1
``reference_test.generator``) and by the stroke-lnm end-to-end test (the bundle).

    python steps/flnm-fast/tests/make_reference.py [DEST_DIR]
"""

from __future__ import annotations

import gzip
import hashlib
import math
import sys
from pathlib import Path
from statistics import NormalDist

import nibabel as nib
import numpy as np
import yaml

BUNDLE_ID = "synthetic-normative-fc"
BUNDLE_VERSION = "1.0.0"
FC_FILE = "fc_row_subject_col.npy"
SUBJECTS_FILE = "normative_subjects.csv"
PARCELLATION_FILE = "standard/schaefer1000_tian54_2mm.nii.gz"

N_SUBJECTS = 2100  # at least 1000 of each sex: the engine default control_count is 1000
# Parcels are the blocks of a 3 x 3 x 3 partition of a box of the working grid (voxel indices).
X_EDGES = (10, 35, 60, 81)
Y_EDGES = (10, 45, 75, 101)
Z_EDGES = (10, 40, 60, 81)
N_ROI = 27

LESION_CENTRE = (35, 45, 40)  # on a corner shared by 8 parcels
LESION_RADIUS = 7.0
# The packaged engine always leaves the lowest-numbered lesion-overlapped parcel out of the
# lesion weighting (its overlap code drops the first element of the label list although the
# background label is already excluded). The expected values reproduce that behaviour, so the
# reference test pins the behaviour of the image digest; see README.md, "Known issue".
ENGINE_DROPS_LOWEST_OVERLAPPED_PARCEL = True
REFERENCE_CONTROLS = 16  # splits the group of females of age 62 (tie: subject index)

MASK64 = (1 << 64) - 1


def working_grid(registry_path: Path) -> tuple[tuple[int, ...], np.ndarray]:
    grid = yaml.safe_load(registry_path.read_text(encoding="utf-8"))["grids"][
        "MNI152NLin6Asym-res2"
    ]
    return tuple(grid["shape"]), np.array(grid["affine"], dtype=float)


# --- deterministic numbers: splitmix64 on integers (exact on every platform) ---------------


def _splitmix64(x: np.ndarray) -> np.ndarray:
    x = (x.astype(np.uint64) + np.uint64(0x9E3779B97F4A7C15))
    x = (x ^ (x >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)
    x = (x ^ (x >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)
    return x ^ (x >> np.uint64(31))


def _hash(*keys) -> np.ndarray:
    """Integer hash of broadcastable integer arrays (uint64)."""
    acc = np.uint64(0)
    with np.errstate(over="ignore"):
        for key in keys:
            acc = _splitmix64(acc ^ np.asarray(key, dtype=np.uint64))
    return acc


def _unit(h: np.ndarray) -> np.ndarray:
    """Hash to a float64 in [0, 1) with 24 significant bits (exactly representable)."""
    return (h >> np.uint64(40)).astype(np.float64) / float(1 << 24)


# --- the normative cohort ------------------------------------------------------------------


def subjects() -> list[dict]:
    """Participants indexed by their position on the FC subject axis."""
    out = []
    for s in range(N_SUBJECTS):
        age = 40 + int(_hash(s, 1) % np.uint64(41))
        sex = "F" if int(_hash(s, 2) % np.uint64(2)) == 0 else "M"  # roughly half each
        out.append({"subject_index": s, "eid": f"synthetic-{s:04d}", "age": age, "sex": sex})
    return out


def subjects_csv(rows: list[dict]) -> str:
    """CSV rows in a hash-permuted order, so that row order is not the subject axis order."""
    order = sorted(rows, key=lambda r: int(_hash(r["subject_index"], 3)))
    lines = ["subject_index,eid,age,sex"]
    lines += [f"{r['subject_index']},{r['eid']},{r['age']},{r['sex']}" for r in order]
    return "\n".join(lines) + "\n"


def parcellation(shape: tuple[int, ...]) -> np.ndarray:
    """Labels 1..27 inside the box, 0 outside (int16)."""
    labels = np.zeros(shape, dtype=np.int16)
    for ix in range(3):
        for iy in range(3):
            for iz in range(3):
                label = 1 + ix * 9 + iy * 3 + iz
                labels[
                    X_EDGES[ix] : X_EDGES[ix + 1],
                    Y_EDGES[iy] : Y_EDGES[iy + 1],
                    Z_EDGES[iz] : Z_EDGES[iz + 1],
                ] = label
    return labels


def fc_array(constant_target: int | None = None) -> np.ndarray:
    """FC[roi, subject, roi] float32; ``constant_target`` (0-based) gets zero variance."""
    roi = np.arange(N_ROI)[:, None, None]
    sub = np.arange(N_SUBJECTS)[None, :, None]
    tgt = np.arange(N_ROI)[None, None, :]
    noise = _unit(_hash(roi, sub, tgt, 4)) - 0.5  # [-0.5, 0.5)
    shift = (_unit(_hash(roi, tgt, 5)) - 0.5) * 0.3  # per roi-target pair, in [-0.15, 0.15)
    fc = (0.6 * noise + shift).astype(np.float32)
    if constant_target is not None:
        fc[:, :, constant_target] = np.float32(0.25)
    return fc


def lesion_mask(shape: tuple[int, ...]) -> np.ndarray:
    ii, jj, kk = np.indices(shape)
    d2 = (
        (ii - LESION_CENTRE[0]) ** 2
        + (jj - LESION_CENTRE[1]) ** 2
        + (kk - LESION_CENTRE[2]) ** 2
    )
    return (d2 <= LESION_RADIUS**2).astype(np.uint8)


# --- independent expected result (standard library) ----------------------------------------


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction of the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 500):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def _betainc(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta function I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    front = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def two_sided_p(t: float, df: int) -> float:
    """P(|T| >= |t|) for Student's t with ``df`` degrees of freedom."""
    return _betainc(df / 2.0, 0.5, df / (df + t * t))


def signed_z(t: float, df: int) -> float:
    p = min(max(two_sided_p(t, df), sys.float_info.min), 1.0)
    z = -NormalDist().inv_cdf(p / 2.0)  # the upper-tail quantile, accurate for tiny p
    return math.copysign(z, t)


def select_controls(rows: list[dict], age: float, sex: str, count: int) -> list[int]:
    """Subject indices of the ``count`` same-sex participants nearest in age (ties: age, index)."""
    same = sorted(
        (abs(r["age"] - age), r["age"], r["subject_index"]) for r in rows if r["sex"] == sex
    )
    return sorted(index for _d, _a, index in same[:count])


def expected_zmap(
    labels: np.ndarray,
    mask: np.ndarray,
    fc: np.ndarray,
    rows: list[dict],
    *,
    age: float,
    sex: str,
    count: int,
    drop_lowest: bool = ENGINE_DROPS_LOWEST_OVERLAPPED_PARCEL,
) -> np.ndarray:
    """Z-map by the definition of the method, one scalar at a time.

    ``drop_lowest`` reproduces the engine's omission of the lowest overlapped parcel.
    """
    in_lesion = labels[mask > 0]
    counts = np.bincount(in_lesion[in_lesion > 0], minlength=N_ROI + 1)
    overlapped = [r for r in range(1, N_ROI + 1) if counts[r]]
    if drop_lowest:
        overlapped = overlapped[1:]
    total = sum(int(counts[r]) for r in overlapped)
    weights = {r: int(counts[r]) / total for r in overlapped}
    controls = select_controls(rows, age, sex, count)
    z = np.full(N_ROI, np.nan)
    for target in range(N_ROI):
        profile = [
            sum(w * float(fc[r - 1, c, target]) for r, w in weights.items()) for c in controls
        ]
        n = len(profile)
        mean = math.fsum(profile) / n
        var = math.fsum((x - mean) ** 2 for x in profile) / (n - 1)
        if var > 0.0:
            z[target] = signed_z(mean / (math.sqrt(var) / math.sqrt(n)), n - 1)
    out = np.zeros(labels.shape, dtype=np.float32)
    inside = labels > 0
    out[inside] = z[labels[inside] - 1].astype(np.float32)
    return out


# --- writing -------------------------------------------------------------------------------


def write_nifti(path: Path, data: np.ndarray, affine: np.ndarray) -> None:
    """Gzip NIfTI with a fixed gzip header (no modification time, no file name)."""
    img = nib.Nifti1Image(data, affine)
    img.set_sform(affine, code=4)
    img.set_qform(affine, code=4)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
            gz.write(img.to_bytes())


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_bundle(
    root: Path, shape: tuple[int, ...], affine: np.ndarray, constant_target: int | None = None
) -> list[dict]:
    """Write the synthetic bundle into ``root``; returns the participant rows."""
    rows = subjects()
    root.mkdir(parents=True, exist_ok=True)
    np.save(root / FC_FILE, fc_array(constant_target))
    (root / SUBJECTS_FILE).write_bytes(subjects_csv(rows).encode("utf-8"))
    write_nifti(root / PARCELLATION_FILE, parcellation(shape), affine)
    files = [
        {"path": rel, "sha256": _sha256(root / rel), "size_bytes": (root / rel).stat().st_size}
        for rel in sorted([FC_FILE, SUBJECTS_FILE, PARCELLATION_FILE], key=lambda p: p.encode())
    ]
    digest = hashlib.sha256(
        "".join(f"{f['sha256']}  {f['path']}\n" for f in files).encode("utf-8")
    ).hexdigest()
    n_female = sum(r["sex"] == "F" for r in rows)
    doc = {
        "contract_version": "1.1",
        "kind": "Bundle",
        "id": BUNDLE_ID,
        "version": BUNDLE_VERSION,
        "type": "normative-fc",
        "title": "Synthetic normative functional connectivity (CI fixture)",
        "description": (
            "27 block parcels on the 2 mm MNI152NLin6Asym grid and hash-generated parcel-by-"
            "parcel connectivity of 2100 synthetic participants with sex and age. Same layout as "
            "a real normative-fc bundle. For CI and demos only."
        ),
        "synthetic": True,
        "licence": {
            "spdx": "CC0-1.0",
            "redistributable": True,
            "notes": "Generated by steps/flnm-fast/tests/make_reference.py; no real data.",
        },
        "metadata": {
            "n_participants": N_SUBJECTS,
            "n_female": n_female,
            "n_male": N_SUBJECTS - n_female,
            "n_parcels": N_ROI,
            "layout": "fc-row-subject-col-v1",
        },
        "content_digest": "sha256:" + digest,
        "files": files,
    }
    (root / "bundle.yaml").write_bytes(
        yaml.safe_dump(doc, sort_keys=False, width=100).encode("utf-8")
    )
    return rows


def generate(dest: Path, registry_path: Path) -> dict:
    shape, affine = working_grid(registry_path)
    rows = write_bundle(dest / "bundle", shape, affine)
    mask = lesion_mask(shape)
    write_nifti(dest / "lesion_mask.nii.gz", mask, affine)
    expected = expected_zmap(
        parcellation(shape), mask, fc_array(), rows, age=62.0, sex="F", count=REFERENCE_CONTROLS
    )
    write_nifti(dest / "expected" / "fdisconnection_map.nii.gz", expected, affine)
    return {"lesion_voxels": int(mask.sum()), "parcels": N_ROI, "participants": N_SUBJECTS}


def main() -> None:
    here = Path(__file__).resolve().parent
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "reference"
    registry = here.parents[2] / "contracts" / "artefact-types.yaml"
    print(generate(dest, registry))


if __name__ == "__main__":
    main()
