"""Generate the synthetic fixtures: ``python -m fixtures.generate --out <dir>``.

Everything is derived from fixed seeds and closed-form geometry; no network
access and no third-party data. Output tree::

    cases/<case>/ct.nii.gz                 ct_native
    cases/<case>/lesion_mask_native.nii.gz lesion_mask_native
    cases/<case>/lesion_mask_mni.nii.gz    lesion_mask_mni (ground truth)
    cases/<case>/truth.json                transform and lesion parameters
    mni_masks/<name>.nii.gz                lesion_mask_mni
    mni_masks/index.tsv                    volume and side of every mask
    bundles/toy-normative-connectome/1.0.0/   (bundle.yaml + files)
    bundles/toy-tract-atlas/1.0.0/            (bundle.yaml + files)
    CHECKSUMS.json                         file and payload SHA-256 of everything
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import yaml

from . import phantom
from .io import (
    MNI2_AFFINE,
    MNI2_SHAPE,
    payload_sha256,
    sha256_bytes,
    sha256_file,
    write_json,
    write_nifti_gz,
    write_npz,
    write_text,
)

SEED = 20260101
CONTRACT_VERSION = "1.0"
BUNDLE_VERSION = "1.0.0"

# Lesion specs: ellipsoid parts in MNI mm. "side" is the hemisphere (RAS: +x right).
CASES = {
    "case-01": {
        "side": "left",
        "parts": [{"centre": [-38, -15, 15], "axes": [16, 13, 14]}],
    },
    "case-02": {
        "side": "right",
        "parts": [{"centre": [32, -5, 25], "axes": [7, 9, 7]}],
    },
}
MNI_MASKS = {
    "lesion_left_small": ("left", [{"centre": [-30, -20, 20], "axes": [7, 7, 7]}]),
    "lesion_right_small": ("right", [{"centre": [32, -5, 25], "axes": [6, 8, 6]}]),
    "lesion_left_medium": ("left", [{"centre": [-38, -15, 15], "axes": [16, 13, 14]}]),
    "lesion_right_medium": ("right", [{"centre": [40, -30, 10], "axes": [14, 15, 13]}]),
    "lesion_left_large": ("left", [{"centre": [-35, -25, 10], "axes": [30, 26, 22]}]),
    "lesion_right_large": ("right", [{"centre": [35, -20, 12], "axes": [32, 28, 24]}]),
    "lesion_right_blob": (
        "right",
        [
            {"centre": [30, -40, 20], "axes": [10, 12, 10]},
            {"centre": [40, -20, 25], "axes": [9, 9, 12]},
        ],
    ),
}
AGE_BANDS = [(45, 54), (55, 64), (65, 74), (75, 85)]
N_PARCELS_PER_HEMI = 50
N_PARTICIPANTS = 20
RANK = 6


def _r6(x) -> list:
    return [round(float(v), 6) for v in np.ravel(x)]


def volume_ml(mask: np.ndarray) -> float:
    return round(float(mask.sum()) * 8.0 / 1000.0, 3)  # 2 mm voxels = 8 mm^3


# --- (a) synthetic patient CTs --------------------------------------------------


def make_cases(out: Path) -> None:
    for i, (case, spec) in enumerate(sorted(CASES.items())):
        rng = np.random.default_rng([SEED, 1, i])
        p = phantom.make_patient(rng, spec)
        d = out / "cases" / case
        write_nifti_gz(d / "ct.nii.gz", p["ct"], p["affine"], mni=False)
        write_nifti_gz(
            d / "lesion_mask_native.nii.gz", p["mask"], p["affine"], mni=False
        )
        write_nifti_gz(
            d / "lesion_mask_mni.nii.gz", p["mni_mask"], MNI2_AFFINE, mni=True
        )
        tr = p["transform"]
        n2m = (
            np.linalg.inv(tr["g"]) @ p["affine"]
        )  # affine part of native voxel -> MNI mm
        native_ml = float(p["mask"].sum()) * float(np.prod(phantom.CT_VOX)) / 1000.0
        write_json(
            d / "truth.json",
            {
                "description": "Synthetic case; ground truth of the random transform and lesion.",
                "lesion": {
                    "side": spec["side"],
                    "parts": spec["parts"],
                    "volume_ml_mni": volume_ml(p["mni_mask"]),
                    "volume_ml_native": round(native_ml, 3),
                },
                "native_voxel_to_scanner_affine": [_r6(r) for r in p["affine"]],
                "native_voxel_to_mni_affine_part": [_r6(r) for r in n2m],
                "rotation_deg": _r6(tr["angles_deg"]),
                "scale": _r6(tr["scale"]),
                "shift_mm": _r6(tr["shift_mm"]),
                "deformation_modes": [
                    {
                        "amp_mm": _r6(m["amp"]),
                        "k_cycles_per_mm": _r6(m["k"]),
                        "phase": round(float(m["phase"]), 6),
                    }
                    for m in tr["modes"]
                ],
            },
        )


# --- (b) MNI lesion masks -------------------------------------------------------


def make_mni_masks(out: Path) -> None:
    rows = ["name\tside\tvolume_ml\tn_voxels"]
    for name, (side, parts) in sorted(MNI_MASKS.items()):
        m = phantom.lesion_mask_on_mni2({"parts": parts})
        write_nifti_gz(out / "mni_masks" / f"{name}.nii.gz", m, MNI2_AFFINE, mni=True)
        rows.append(f"{name}\t{side}\t{volume_ml(m)}\t{int(m.sum())}")
    write_text(out / "mni_masks" / "index.tsv", "\n".join(rows) + "\n")


# --- bundles --------------------------------------------------------------------


def content_digest(files: list[dict]) -> str:
    text = "".join(
        f"{f['sha256']}  {f['path']}\n"
        for f in sorted(files, key=lambda f: f["path"].encode())
    )
    return "sha256:" + sha256_bytes(text.encode("utf-8"))


def finish_bundle(root: Path, header: dict) -> None:
    files = []
    # Sort on the POSIX string: Path ordering is case-insensitive on Windows.
    for p in sorted(root.rglob("*"), key=lambda q: q.relative_to(root).as_posix()):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and rel != "bundle.yaml":
            files.append(
                {"path": rel, "sha256": sha256_file(p), "size_bytes": p.stat().st_size}
            )
    doc = dict(header)
    doc["content_digest"] = content_digest(files)
    doc["files"] = files
    write_text(root / "bundle.yaml", yaml.safe_dump(doc, sort_keys=False, width=88))


# --- (c) normative connectome bundle --------------------------------------------


def make_connectome_bundle(out: Path) -> None:
    root = out / "bundles" / "toy-normative-connectome" / BUNDLE_VERSION
    rng = np.random.default_rng([SEED, 2])
    pts = phantom.grid_points(MNI2_SHAPE, MNI2_AFFINE)
    brain = phantom.in_brain(pts)

    # Synthetic parcellation: mirror-symmetric Voronoi cells of random seeds.
    cand = pts[brain & (pts[:, 0] >= 4)]
    seeds_r = cand[rng.choice(len(cand), N_PARCELS_PER_HEMI, replace=False)]
    seeds_r = seeds_r[np.lexsort((seeds_r[:, 2], seeds_r[:, 1], seeds_r[:, 0]))]
    seeds_l = seeds_r * np.array([-1.0, 1.0, 1.0])
    seeds = np.concatenate([seeds_l, seeds_r])  # labels 1..50 left, 51..100 right
    n_parcels = len(seeds)
    bp = pts[brain]
    nearest = np.empty(len(bp), dtype=np.int64)
    for s in range(0, len(bp), 20000):
        d = ((bp[s : s + 20000, None, :] - seeds[None]) ** 2).sum(-1)
        nearest[s : s + 20000] = d.argmin(1)
    atlas = np.zeros(len(pts), dtype=np.uint8)
    atlas[brain] = (nearest + 1).astype(np.uint8)
    write_nifti_gz(
        root / "parcellation" / "atlas.nii.gz",
        atlas.reshape(MNI2_SHAPE),
        MNI2_AFFINE,
        mni=True,
    )
    labels = ["index\tlabel\themisphere"]
    for k in range(n_parcels):
        hemi = "L" if k < N_PARCELS_PER_HEMI else "R"
        labels.append(f"{k + 1}\t{hemi}_{k % N_PARCELS_PER_HEMI + 1:03d}\t{hemi}")
    write_text(root / "parcellation" / "labels.tsv", "\n".join(labels) + "\n")

    # Participants: balanced sex, ages 45-85, one age band each.
    sex = np.array(["F"] * (N_PARTICIPANTS // 2) + ["M"] * (N_PARTICIPANTS // 2))
    sex = sex[rng.permutation(N_PARTICIPANTS)]
    age = rng.integers(45, 86, N_PARTICIPANTS)
    age[0], age[1] = 45, 85  # pin the range stated in the metadata
    ids = [f"P{i + 1:03d}" for i in range(N_PARTICIPANTS)]
    rows = ["participant_id\tsex\tage\tage_band"]
    for pid, s, a in zip(ids, sex, age, strict=True):
        band = next(f"{lo}-{hi}" for lo, hi in AGE_BANDS if lo <= a <= hi)
        rows.append(f"{pid}\t{s}\t{int(a)}\t{band}")
    write_text(root / "participants.tsv", "\n".join(rows) + "\n")

    # Random low-rank connectivity: shared factor + individual noise + small
    # sex and age effects. Symmetric, zero diagonal, rounded to 3 decimals.
    shared = rng.normal(size=(n_parcels, RANK))
    sex_dir = rng.normal(size=(n_parcels, RANK))
    mats = np.empty((N_PARTICIPANTS, n_parcels, n_parcels), dtype=np.float32)
    for i in range(N_PARTICIPANTS):
        f = shared + 0.3 * rng.normal(size=shared.shape)
        f = f + (0.15 if sex[i] == "F" else -0.15) * sex_dir
        w = (f @ f.T) / RANK * (1.0 - 0.004 * (age[i] - 65))
        np.fill_diagonal(w, 0.0)
        mats[i] = np.round(w, 3).astype(np.float32)
    write_npz(
        root / "connectomes.npz",
        connectivity=mats,
        participant_id=np.array(ids),
        parcel_index=np.arange(1, n_parcels + 1),
    )

    write_text(
        root / "README.md",
        f"""# Toy normative connectome (synthetic)

Placeholder layout, to be replaced once the flnm-fast input format is known.

- `connectomes.npz`: `connectivity` float32 (participants, parcels, parcels),
  symmetric, zero diagonal; `participant_id` (same order as `participants.tsv`);
  `parcel_index` (1..{n_parcels}).
- `participants.tsv`: `participant_id`, `sex` (F/M), `age`, `age_band`.
- `parcellation/atlas.nii.gz`: uint8 labels 1..{n_parcels} on the 2 mm MNI152NLin6Asym
  working grid (synthetic mirror-symmetric Voronoi parcellation, labels 1-50 left,
  51-100 right); 0 is background.
- `parcellation/labels.tsv`: `index`, `label`, `hemisphere`.

All data are random and carry no biological meaning. Generated by
`python -m fixtures.generate`; see `fixtures/LICENCES.md` in the platform repository.
""",
    )
    n_f = int((sex == "F").sum())
    finish_bundle(
        root,
        {
            "contract_version": CONTRACT_VERSION,
            "kind": "Bundle",
            "id": "toy-normative-connectome",
            "version": BUNDLE_VERSION,
            "type": "normative-connectome",
            "title": "Toy normative connectome (synthetic)",
            "description": (
                "Synthetic parcellation plus random low-rank connectivity matrices "
                "for 20 synthetic participants with sex and age labels. For CI and "
                "demos only."
            ),
            "synthetic": True,
            "licence": {
                "spdx": "CC0-1.0",
                "redistributable": True,
                "notes": "Generated data; no public ingredient. See fixtures/LICENCES.md.",
            },
            "metadata": {
                "n_participants": N_PARTICIPANTS,
                "n_female": n_f,
                "n_male": N_PARTICIPANTS - n_f,
                "age_range": f"{int(age.min())}-{int(age.max())}",
                "n_parcels": n_parcels,
                "layout": "placeholder-v0",
            },
        },
    )


# --- (d) tract bundle -----------------------------------------------------------

TRACTS = {  # quadratic-Bezier control points (MNI mm)
    "toy_cst_left": [(-36, -25, 62), (-22, -22, 20), (-10, -24, -25)],
    "toy_cst_right": [(36, -25, 62), (22, -22, 20), (10, -24, -25)],
    "toy_arcuate_left": [(-50, -45, 12), (-60, -5, 50), (-46, 22, 24)],
    "toy_cc_body": [(-48, -12, 34), (0, -12, 66), (48, -12, 34)],
}
N_STREAMLINES = 60
N_POINTS = 30


def _to_voxel(points: np.ndarray) -> np.ndarray:
    return np.rint((points - MNI2_AFFINE[:3, 3]) / np.diag(MNI2_AFFINE)[:3]).astype(int)


def make_tract_bundle(out: Path) -> None:
    root = out / "bundles" / "toy-tract-atlas" / BUNDLE_VERSION
    rng = np.random.default_rng([SEED, 3])
    t = np.linspace(0.0, 1.0, N_POINTS)[:, None]
    all_pts, offsets, tract_of = [], [0], []
    rows = ["tract\tmask_file\tn_streamlines"]
    for ti, (name, ctrl) in enumerate(sorted(TRACTS.items())):
        p0, p1, p2 = (np.array(p, float) for p in ctrl)
        voxels = np.zeros(MNI2_SHAPE, dtype=np.uint8)
        for _ in range(N_STREAMLINES):
            jit = rng.normal(0.0, 2.5, (3, 3))
            a, b, c = p0 + jit[0], p1 + 2 * jit[1], p2 + jit[2]
            line = (1 - t) ** 2 * a + 2 * (1 - t) * t * b + t**2 * c
            line = np.round(line, 2).astype(np.float32)
            all_pts.append(line)
            offsets.append(offsets[-1] + N_POINTS)
            tract_of.append(ti)
            voxels[tuple(_to_voxel(line).T)] = 1
        write_nifti_gz(
            root / "tracts" / f"{name}.nii.gz", voxels, MNI2_AFFINE, mni=True
        )
        rows.append(f"{name}\ttracts/{name}.nii.gz\t{N_STREAMLINES}")
    write_text(root / "tracts.tsv", "\n".join(rows) + "\n")
    write_npz(
        root / "streamlines.npz",
        points=np.concatenate(all_pts),
        offsets=np.array(offsets, dtype=np.int64),
        tract_index=np.array(tract_of, dtype=np.int16),
        tract_names=np.array(sorted(TRACTS)),
    )
    write_text(
        root / "README.md",
        """# Toy tract atlas (synthetic)

Four synthetic tracts, defined as bundles of curved streamlines in MNI152NLin6Asym
millimetre coordinates. Placeholder layout until the tract-wise disconnection step
fixes the real one.

- `tracts.tsv`: `tract`, `mask_file`, `n_streamlines`.
- `tracts/<tract>.nii.gz`: binary voxelisation of the tract on the 2 mm working grid.
- `streamlines.npz`: `points` float32 (N, 3) concatenated streamlines; `offsets` int64
  (S + 1), streamline s is `points[offsets[s]:offsets[s+1]]`; `tract_index` int16 (S)
  indexes `tract_names` (sorted tract names).

Tract disconnection of a lesion = fraction of a tract's streamlines with at least one
point inside the lesion mask.
""",
    )
    finish_bundle(
        root,
        {
            "contract_version": CONTRACT_VERSION,
            "kind": "Bundle",
            "id": "toy-tract-atlas",
            "version": BUNDLE_VERSION,
            "type": "tract-atlas",
            "title": "Toy tract atlas (synthetic)",
            "description": (
                "Four synthetic streamline bundles with voxel masks. For CI and demos only."
            ),
            "synthetic": True,
            "licence": {
                "spdx": "CC0-1.0",
                "redistributable": True,
                "notes": "Generated data; no public ingredient. See fixtures/LICENCES.md.",
            },
            "metadata": {
                "n_tracts": len(TRACTS),
                "n_streamlines_per_tract": N_STREAMLINES,
            },
        },
    )


def tract_disconnection(root: Path, lesion: np.ndarray) -> dict[str, float]:
    """Reference tract disconnection of a working-grid lesion mask (for tests)."""
    z = np.load(root / "streamlines.npz")
    pts, off, idx, names = z["points"], z["offsets"], z["tract_index"], z["tract_names"]
    hit = lesion[tuple(_to_voxel(pts).T)] > 0
    per_line = np.add.reduceat(hit.astype(int), off[:-1]) > 0
    return {str(n): float(per_line[idx == k].mean()) for k, n in enumerate(names)}


# --- driver ---------------------------------------------------------------------


def checksums(out: Path) -> dict:
    entries = {}
    for p in sorted(out.rglob("*")):
        rel = p.relative_to(out).as_posix()
        if p.is_file() and rel != "CHECKSUMS.json":
            entries[rel] = {
                "sha256": sha256_file(p),
                "payload_sha256": payload_sha256(p),
                "size_bytes": p.stat().st_size,
            }
    return entries


def generate(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    make_cases(out)
    make_mni_masks(out)
    make_connectome_bundle(out)
    make_tract_bundle(out)
    write_json(
        out / "CHECKSUMS.json",
        {
            "note": (
                "payload_sha256 is the SHA-256 of the decompressed content and is the "
                "cross-platform reproducibility check; sha256 is of the stored bytes."
            ),
            "seed": SEED,
            "files": checksums(out),
        },
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--out", type=Path, required=True, help="output directory")
    args = ap.parse_args(argv)
    generate(args.out)
    print(f"fixtures written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
