"""Synthetic reference dataset of the slnm-coarse step (nothing is committed, nothing is real).

Writes, deterministically (fixed formulas, integer hashes, no random generator, no time
stamps):

* ``reference/bundle/``: a synthetic ``slnm-coarse-cache`` bundle: ``participants.csv`` (nine
  synthetic participants over the four sites) and ``cache/``, a portable slnm-coarse cache root
  with the common atlas (eight block parcels with gapped labels up to 2966 on the 2 mm working
  grid) and, per participant, a native atlas on a smaller native grid (a crop of the working
  grid, block boundary shifted per participant), a zero B0 image, identity forward transforms
  (an ITK affine in MATLAB v4 format and a zero displacement field), a hash-generated
  streamline-to-parcel index with SIFT2 weights, and ``metadata.json``. Parcel 2900 has no
  streamline at the WashU participants (no support at one site) and parcel 2966 has none at
  all, so two parcels have no valid group estimate;
* ``reference/lesion_mask.nii.gz``: a ball on the corner shared by the eight parcels;
* ``reference/expected/``: the group map (NaN for parcels without a valid estimate) and the
  validity mask, computed here with numpy from the definition of the method, not by running
  the author's code or the step.

Used by the step tests, by ``stroke-step-reftest`` (contract 1.1 ``reference_test.generator``)
and by the CI job.

    python steps/slnm-coarse/tests/make_reference.py [DEST_DIR]
"""

from __future__ import annotations

import gzip
import hashlib
import json
import struct
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml

BUNDLE_ID = "synthetic-slnm-coarse-cache"
BUNDLE_VERSION = "1.0.0"
ATLAS_FILENAME = "firsT_2566_MWMA400_min20.nii.gz"
N_LABELS = 2966
SITES = ("MGH", "UCLA", "UMinn", "WashU")
SUFFIX = "_DiffusionRecommended"

# (subject_label, site): clearly synthetic labels in the form slnm-coarse accepts
PARTICIPANTS = [
    (f"HCASYNTH{i:02d}_V1_MR", site)
    for i, site in enumerate(
        ("MGH", "UCLA", "UMinn", "WashU", "MGH", "UCLA", "UMinn", "WashU", "MGH"), start=1
    )
]
# Eight block parcels (2 x 2 x 2) inside a box of the working grid (voxel indices)
LABELS = (3, 40, 512, 1001, 2566, 2700, 2900, 2966)
X_EDGES, Y_EDGES, Z_EDGES = (20, 45, 70), (25, 55, 85), (20, 45, 70)
NO_SUPPORT_AT_WASHU = 2900
NO_STREAMLINES = 2966
# Native grid: a crop of the working grid (same voxel size, other origin and shape)
NATIVE_OFFSET = (10, 15, 10)
NATIVE_SHAPE = (70, 80, 70)
LESION_CENTRE = (45, 55, 45)
LESION_RADIUS = 6.0

MASK64 = (1 << 64) - 1


def working_grid(registry_path: Path) -> tuple[tuple[int, ...], np.ndarray]:
    grid = yaml.safe_load(registry_path.read_text(encoding="utf-8"))["grids"][
        "MNI152NLin6Asym-res2"
    ]
    return tuple(grid["shape"]), np.array(grid["affine"], dtype=float)


def _hash(*keys: int) -> int:
    """splitmix64 over integers (exact on every platform)."""
    acc = 0
    for key in keys:
        x = (acc ^ (key & MASK64)) + 0x9E3779B97F4A7C15 & MASK64
        x = (x ^ (x >> 30)) * 0xBF58476D1CE4E5B9 & MASK64
        x = (x ^ (x >> 27)) * 0x94D049BB133111EB & MASK64
        acc = x ^ (x >> 31)
    return acc


# --- the cohort -----------------------------------------------------------------------------


def block_atlas(shape: tuple[int, ...], x_shift: int = 0) -> np.ndarray:
    """Labels of LABELS in the 2 x 2 x 2 blocks, 0 outside (int16); ``x_shift`` moves the
    inner boundary along x (a participant's native atlas)."""
    xs = (X_EDGES[0], X_EDGES[1] + x_shift, X_EDGES[2])
    labels = np.zeros(shape, dtype=np.int16)
    for ix in range(2):
        for iy in range(2):
            for iz in range(2):
                labels[
                    xs[ix] : xs[ix + 1], Y_EDGES[iy] : Y_EDGES[iy + 1], Z_EDGES[iz] : Z_EDGES[iz + 1]
                ] = LABELS[ix * 4 + iy * 2 + iz]
    return labels


def native_affine(affine: np.ndarray) -> np.ndarray:
    shift = np.eye(4)
    shift[:3, 3] = NATIVE_OFFSET
    return affine @ shift


def crop(volume: np.ndarray) -> np.ndarray:
    (x, y, z), (nx, ny, nz) = NATIVE_OFFSET, NATIVE_SHAPE
    return volume[x : x + nx, y : y + ny, z : z + nz]


def native_atlas(shape: tuple[int, ...], index: int) -> np.ndarray:
    return crop(block_atlas(shape, x_shift=index % 3 - 1))


def tracks(index: int, site: str) -> tuple[list[tuple[int, ...]], list[float]]:
    """Parcels visited by each streamline of a participant and its SIFT2 weight (multiples of
    1/16, exact in binary). Every candidate parcel is visited at least once."""
    candidates = [
        r for r in LABELS if r != NO_STREAMLINES and not (r == NO_SUPPORT_AT_WASHU and site == "WashU")
    ]
    n = 40 + 3 * index
    visits, weights = [], []
    for t in range(n):
        k = 1 + _hash(index, t, 1) % 3
        chosen = {candidates[t % len(candidates)]} if t < len(candidates) else set()
        j = 0
        while len(chosen) < k:
            chosen.add(candidates[_hash(index, t, 2, j) % len(candidates)])
            j += 1
        visits.append(tuple(sorted(chosen)))
        weights.append((1 + _hash(index, t, 3) % 64) / 16)
    return visits, weights


def lesion_mask(shape: tuple[int, ...]) -> np.ndarray:
    ii, jj, kk = np.indices(shape)
    d2 = (ii - LESION_CENTRE[0]) ** 2 + (jj - LESION_CENTRE[1]) ** 2 + (kk - LESION_CENTRE[2]) ** 2
    return (d2 <= LESION_RADIUS**2).astype(np.uint8)


# --- independent expected result ----------------------------------------------------------------


def participant_scores(
    atlas: np.ndarray, lesion: np.ndarray, visits: list[tuple[int, ...]], weights: list[float]
) -> tuple[dict[int, float], set[int]]:
    """Scores and supported parcels of one participant, by the definition of the method:
    p[r] = fraction of parcel r's native voxels in the lesion; a streamline is hit with
    probability 1 - prod over its parcels (1 - p[r]); the score of r is the weighted mean of
    the hit probabilities of the streamlines visiting r."""
    p = {r: float(np.mean(lesion[atlas == r] > 0)) if np.any(atlas == r) else 0.0 for r in LABELS}
    hit = [1.0 - float(np.prod([1.0 - p[r] for r in v])) for v in visits]
    scores, supported = {}, set()
    for r in LABELS:
        mine = [i for i, v in enumerate(visits) if r in v]
        total = sum(weights[i] for i in mine)
        if total > 0:
            supported.add(r)
            scores[r] = sum(weights[i] * hit[i] for i in mine) / total
    return scores, supported


def group_values(per_participant: list[tuple[str, dict[int, float], set[int]]]) -> dict[int, float]:
    """Equal-weight mean of the four site means, for parcels supported at every site by more
    than four participants in total (others are absent: no valid estimate)."""
    out = {}
    for r in LABELS:
        by_site = {s: [sc[r] for site, sc, sup in per_participant if site == s and r in sup] for s in SITES}
        n = sum(len(v) for v in by_site.values())
        if all(by_site[s] for s in SITES) and n > len(SITES):
            out[r] = float(np.mean([np.mean(by_site[s]) for s in SITES]))
    return out


def expected_outputs(shape: tuple[int, ...]) -> tuple[np.ndarray, np.ndarray]:
    lesion_native = crop(lesion_mask(shape))
    rows = []
    for index, (_, site) in enumerate(PARTICIPANTS):
        visits, weights = tracks(index, site)
        scores, supported = participant_scores(native_atlas(shape, index), lesion_native, visits, weights)
        rows.append((site, scores, supported))
    values = group_values(rows)
    atlas = block_atlas(shape)
    result = np.zeros(shape, dtype=np.float32)
    valid = np.zeros(shape, dtype=np.uint8)
    for r in LABELS:
        if r in values:
            result[atlas == r] = values[r]
            valid[atlas == r] = 1
        else:
            result[atlas == r] = np.nan
    return result, valid


# --- writing --------------------------------------------------------------------------------


def write_nifti(path: Path, data: np.ndarray, affine: np.ndarray, intent: str | None = None) -> None:
    """Gzip NIfTI with a fixed gzip header (no modification time, no file name)."""
    img = nib.Nifti1Image(data, affine)
    img.set_sform(affine, code=4)
    img.set_qform(affine, code=4)
    if intent:
        img.header.set_intent(intent)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as gz:
            gz.write(img.to_bytes())


def write_itk_identity_affine(path: Path) -> None:
    """ITK affine transform file (MATLAB v4, as antsRegistration writes *GenericAffine.mat):
    variables AffineTransform_double_3_3 (row-major matrix then translation) and fixed (the
    centre), little-endian doubles, column vectors."""
    variables = (
        ("AffineTransform_double_3_3", [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0, 0, 0, 0]),
        ("fixed", [0.0, 0.0, 0.0]),
    )
    out = bytearray()
    for name, values in variables:
        encoded = name.encode("ascii") + b"\0"
        out += struct.pack("<5i", 0, len(values), 1, 0, len(encoded))
        out += encoded
        out += struct.pack(f"<{len(values)}d", *values)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(out))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_participant(cache: Path, index: int, label: str, site: str, shape, affine, atlas_sha: str) -> None:
    root = cache / f"{label}{SUFFIX}"
    nat_affine = native_affine(affine)
    write_nifti(root / "atlas_dwispace.nii.gz", native_atlas(shape, index), nat_affine)
    write_nifti(root / "dwi_bzero.nii.gz", np.zeros(NATIVE_SHAPE, dtype=np.int16), nat_affine)
    write_itk_identity_affine(root / "dwireg" / "standard2dwi0GenericAffine.mat")
    write_nifti(
        root / "dwireg" / "standard2dwi1Warp.nii.gz",
        np.zeros((*NATIVE_SHAPE, 1, 3), dtype=np.float32),
        nat_affine,
        intent="vector",
    )
    visits, weights = tracks(index, site)
    (root / "streamlines").mkdir(parents=True, exist_ok=True)
    (root / "streamlines" / "sift2_weights.txt").write_text(
        " ".join(repr(w) for w in weights) + "\n", encoding="ascii"
    )
    per_roi = [[t for t, v in enumerate(visits) if r in v] for r in range(N_LABELS + 1)]
    offsets = np.zeros(N_LABELS + 2, dtype=np.int64)
    offsets[1:] = np.cumsum([len(ids) for ids in per_roi])
    ids = np.array([t for roi in per_roi for t in roi], dtype=np.int64)
    totals = np.array([sum(weights[t] for t in roi) for roi in per_roi], dtype=np.float64)
    (root / "index").mkdir(parents=True, exist_ok=True)
    np.save(root / "index" / "roi_offsets.npy", offsets)
    np.save(root / "index" / "track_ids.npy", ids)
    np.save(root / "index" / "roi_weight_totals.npy", totals)
    metadata = {
        "participant": f"{label}{SUFFIX}",
        "streamlines": len(weights),
        "inputs_mode": "copy",
        "input_fingerprints": {"mni_atlas": {"sha256": atlas_sha}},
        "note": "synthetic; written by steps/slnm-coarse/tests/make_reference.py",
    }
    (root / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")


def write_bundle(root: Path, shape: tuple[int, ...], affine: np.ndarray) -> None:
    cache = root / "cache"
    write_nifti(cache / ATLAS_FILENAME, block_atlas(shape), affine)
    atlas_sha = _sha256(cache / ATLAS_FILENAME)
    lines = ["subject_label,site"] + [f"{label},{site}" for label, site in PARTICIPANTS]
    (root / "participants.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for index, (label, site) in enumerate(PARTICIPANTS):
        write_participant(cache, index, label, site, shape, affine, atlas_sha)
    rels = sorted(
        (p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p.name != "bundle.yaml"),
        key=lambda rel: rel.encode(),
    )
    files = [
        {"path": rel, "sha256": _sha256(root / rel), "size_bytes": (root / rel).stat().st_size}
        for rel in rels
    ]
    digest = hashlib.sha256("".join(f"{f['sha256']}  {f['path']}\n" for f in files).encode()).hexdigest()
    sites = {s: sum(1 for _, site in PARTICIPANTS if site == s) for s in SITES}
    doc = {
        "contract_version": "1.2",
        "kind": "Bundle",
        "id": BUNDLE_ID,
        "version": BUNDLE_VERSION,
        "type": "slnm-coarse-cache",
        "title": "Synthetic slnm-coarse cohort cache (CI fixture)",
        "description": (
            "Nine synthetic participants over the four sites, eight block parcels on the 2 mm "
            "MNI152NLin6Asym grid, identity transforms and hash-generated streamline-to-parcel "
            "incidence. Same layout as a real slnm-coarse-cache bundle. For CI only."
        ),
        "synthetic": True,
        "licence": {
            "spdx": "CC0-1.0",
            "redistributable": True,
            "notes": "Generated by steps/slnm-coarse/tests/make_reference.py; no real data.",
        },
        "metadata": {
            "layout": "slnm-coarse-cache-v1",
            "atlas": f"cache/{ATLAS_FILENAME}",
            "n_participants": len(PARTICIPANTS),
            **{f"n_participants_{s.lower()}": n for s, n in sites.items()},
        },
        "content_digest": "sha256:" + digest,
        "files": files,
    }
    (root / "bundle.yaml").write_bytes(yaml.safe_dump(doc, sort_keys=False, width=100).encode("utf-8"))


def generate(dest: Path, registry_path: Path) -> dict:
    shape, affine = working_grid(registry_path)
    write_bundle(dest / "bundle", shape, affine)
    mask = lesion_mask(shape)
    write_nifti(dest / "lesion_mask.nii.gz", mask, affine)
    result, valid = expected_outputs(shape)
    write_nifti(dest / "expected" / "sdisconnection_map.nii.gz", result, affine)
    write_nifti(dest / "expected" / "sdisconnection_valid_mask.nii.gz", valid, affine)
    return {
        "lesion_voxels": int(mask.sum()),
        "participants": len(PARTICIPANTS),
        "valid_parcels": int(len(np.unique(block_atlas(shape)[valid > 0]))),
    }


def main() -> None:
    here = Path(__file__).resolve().parent
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "reference"
    registry = here.parents[2] / "contracts" / "artefact-types.yaml"
    print(generate(dest, registry))


if __name__ == "__main__":
    main()
