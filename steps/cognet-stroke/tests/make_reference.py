"""Generate the synthetic reference dataset of the step. Nothing here is real data.

Everything lives on the 2 mm MNI152NLin6Asym working grid and is produced from a fixed seed:

* a ``concept-maps`` bundle: a block parcellation of an ellipsoidal "brain", six concepts in three
  domains (each concept is a ball of parcels around a centre, 1 inside and -1 outside), and 100
  null masks per concept (balls of the same radius around random parcels);
* a lesion (ball) and a disconnection map (smooth positive blob around the lesion plus a negative
  lobe elsewhere);
* the expected profile, computed with a deliberately naive implementation that shares no code
  with the step or with the author's ``src/cdr.py``.

    python steps/cognet-stroke/tests/make_reference.py DEST_DIR CONTRACTS_DIR

The data and the expected values are those of step 1.0.0 (same seed, same bytes); only the
expected ``profile_version`` follows the step (``1.1`` since 1.1.0).

writes ``DEST_DIR/bundle/``, ``DEST_DIR/lesion_mask.nii.gz``, ``DEST_DIR/fdisconnection_map.nii.gz``
and ``DEST_DIR/expected/cognet_profile.json``.
"""

from __future__ import annotations

import hashlib
import json
import sys
import zipfile
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml

SEED = 20261113
BLOCK = 9  # parcel edge in voxels
N_NULLS = 1000
PROFILE_VERSION = "1.1"
ALPHA = 0.05
CENTRE = np.array([45.0, 54.0, 36.0])
RADII = np.array([38.0, 50.0, 34.0])
# concept: (domain, centre in voxels, radius in voxels)
CONCEPTS = {
    "motor_left": ("motor", (60, 60, 50), 14.0),
    "motor_right": ("motor", (30, 60, 50), 14.0),
    "visual_early": ("sensory", (45, 20, 36), 14.0),
    "visual_motion": ("sensory", (60, 30, 40), 12.0),
    "attention": ("cognition", (35, 75, 45), 13.0),
    "working_memory": ("cognition", (55, 85, 40), 13.0),
}
LESION_CENTRE = (58, 62, 48)
LESION_RADIUS = 5.0


def working_grid(contracts_dir: Path):
    registry = yaml.safe_load((contracts_dir / "artefact-types.yaml").read_text(encoding="utf-8"))
    grid = registry["grids"]["MNI152NLin6Asym-res2"]
    return tuple(grid["shape"]), np.array(grid["affine"], dtype=float)


def brain(shape) -> np.ndarray:
    ii, jj, kk = np.indices(shape)
    d = ((ii - CENTRE[0]) / RADII[0]) ** 2 + ((jj - CENTRE[1]) / RADII[1]) ** 2
    d = d + ((kk - CENTRE[2]) / RADII[2]) ** 2
    return d <= 1.0


def parcellation(shape) -> np.ndarray:
    ii, jj, kk = np.indices(shape)
    nb = [int(np.ceil(s / BLOCK)) for s in shape]
    label = 1 + (ii // BLOCK) + nb[0] * ((jj // BLOCK) + nb[1] * (kk // BLOCK))
    label = np.where(brain(shape), label, 0)
    # compact, ordered labels 1..P
    ids = np.unique(label[label > 0])
    out = np.zeros(shape, dtype=np.int16)
    for new, old in enumerate(ids, start=1):
        out[label == old] = new
    return out


def centroids(labels: np.ndarray) -> np.ndarray:
    ids = np.unique(labels[labels > 0])
    return np.array([np.argwhere(labels == i).mean(axis=0) for i in ids])


def ball_mask(cent: np.ndarray, centre, radius) -> np.ndarray:
    inside = np.linalg.norm(cent - np.asarray(centre, float), axis=1) <= radius
    return np.where(inside, 1, -1).astype(np.int8)


def write_npz(path: Path, **arrays) -> None:
    """Deterministic .npz (fixed zip timestamps)."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, arr in arrays.items():
            info = zipfile.ZipInfo(f"{name}.npy", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            with zf.open(info, "w") as fh:
                np.lib.format.write_array(fh, np.asarray(arr), allow_pickle=False)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def content_digest(files: list) -> str:
    lines = "".join(f"{f['sha256']}  {f['path']}\n" for f in sorted(files, key=lambda f: f["path"].encode()))
    return "sha256:" + hashlib.sha256(lines.encode("utf-8")).hexdigest()


def write_bundle(dest: Path, affine, labels, cent, rng) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "concepts").mkdir(exist_ok=True)
    nib.save(nib.Nifti1Image(labels, affine), str(dest / "parcellation.nii.gz"))
    rows = ["concept\tdomain"]
    for name, (domain, centre, radius) in CONCEPTS.items():
        rows.append(f"{name}\t{domain}")
        mask = ball_mask(cent, centre, radius)
        nulls = np.stack(
            [ball_mask(cent, cent[rng.integers(len(cent))], radius) for _ in range(N_NULLS)]
        )
        write_npz(dest / "concepts" / f"{name}.npz", mask=mask, nulls=nulls)
    (dest / "concepts.tsv").write_text("\n".join(rows) + "\n", encoding="utf-8")
    files = [
        {"path": p.relative_to(dest).as_posix(), "sha256": sha256(p), "size_bytes": p.stat().st_size}
        for p in sorted(dest.rglob("*"))
        if p.is_file() and p.name != "bundle.yaml"
    ]
    doc = {
        "contract_version": "1.0",
        "kind": "Bundle",
        "id": "synthetic-concept-maps",
        "version": "1.0.0",
        "type": "concept-maps",
        "title": "Synthetic concept maps (CI fixture)",
        "description": "Block parcellation, six ball-shaped concepts in three domains and 100 ball-shaped null masks per concept, generated from a fixed seed by step/tests/make_reference.py. For tests only.",
        "synthetic": True,
        "licence": {"spdx": "CC0-1.0", "redistributable": True, "notes": "Generated data."},
        "metadata": {"n_parcels": int(labels.max()), "n_concepts": len(CONCEPTS), "n_nulls": N_NULLS},
        "content_digest": content_digest(files),
        "files": files,
    }
    (dest / "bundle.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return doc


def inputs(shape, affine):
    ii, jj, kk = np.indices(shape)
    d2 = (ii - LESION_CENTRE[0]) ** 2 + (jj - LESION_CENTRE[1]) ** 2 + (kk - LESION_CENTRE[2]) ** 2
    lesion = (d2 <= LESION_RADIUS**2).astype(np.uint8)
    other = (ii - 30) ** 2 + (jj - 70) ** 2 + (kk - 40) ** 2
    disc = np.exp(-d2 / (2 * 12.0**2)) - 0.3 * np.exp(-other / (2 * 10.0**2))
    disc = (disc * brain(shape)).astype(np.float32)
    return lesion, disc


# ---- expected profile: naive implementation, independent of the step and the author's code ----


def _naive_cdr(mask, values):
    inside = [v for m, v in zip(mask, values) if m == 1]
    outside = [v for m, v in zip(mask, values) if m == -1]
    a, b = sum(inside) / len(inside), sum(outside) / len(outside)
    return a if b == 0 else a / b


def _naive_overlap(mask, lesion):
    return sum(l for m, l in zip(mask, lesion) if m == 1) / sum(lesion)


def _p(obs, nulls_values):
    hits = sum(1 for v in nulls_values if v > obs)
    return max(hits, 1) / len(nulls_values)


def expected_profile(labels, lesion, disc, bundle_doc, bundle_dir: Path, affine) -> dict:
    ids = [int(i) for i in np.unique(labels[labels > 0])]
    pos = [float(np.mean(np.where(disc > 0, disc, 0.0)[labels == i])) for i in ids]
    neg = [float(np.mean(np.where(disc < 0, -disc, 0.0)[labels == i])) for i in ids]
    les = [1.0 if lesion[labels == i].any() else 0.0 for i in ids]
    measures = {"disconnection_positive": pos, "disconnection_negative": neg, "lesion_overlap": les}
    rows = []
    for name, (domain, _c, _r) in CONCEPTS.items():
        with np.load(bundle_dir / "concepts" / f"{name}.npz") as z:
            mask, nulls = z["mask"].tolist(), z["nulls"].tolist()
        row = {"concept": name, "domain": domain}
        for m, vec in measures.items():
            fn = _naive_overlap if m == "lesion_overlap" else _naive_cdr
            score = fn(mask, vec)
            row[m] = {"score": score, "p_value": _p(score, [fn(n, vec) for n in nulls])}
        rows.append(row)

    def correct(items, key):
        for it in items:
            pc = min(1.0, it[key]["p_value"] * len(items))
            it[key]["p_corrected"] = pc
            it[key]["significant"] = pc < ALPHA

    for m in measures:
        correct(rows, m)
    domains = []
    for d in dict.fromkeys(r["domain"] for r in rows):
        members = [r for r in rows if r["domain"] == d]
        entry = {"domain": d, "n_concepts": len(members)}
        for m in measures:
            entry[m] = {
                "score": sum(r[m]["score"] for r in members) / len(members),
                "p_value": min(1.0, min(r[m]["p_value"] for r in members) * len(members)),
            }
        domains.append(entry)
    for m in measures:
        correct(domains, m)
    return {
        "kind": "CognetProfile",
        "profile_version": PROFILE_VERSION,
        "method": {
            "name": "concept-to-disconnection-representation",
            "alpha": ALPHA,
            "correction": "bonferroni",
            "n_nulls": N_NULLS,
            "n_parcels": len(ids),
            "measures": list(measures),
        },
        "bundle": {
            "id": bundle_doc["id"],
            "version": bundle_doc["version"],
            "content_digest": bundle_doc["content_digest"],
            "synthetic": True,
        },
        "concepts": rows,
        "domains": domains,
    }


def generate(dest: Path, contracts_dir: Path) -> dict:
    dest.mkdir(parents=True, exist_ok=True)
    shape, affine = working_grid(contracts_dir)
    rng = np.random.default_rng(SEED)
    labels = parcellation(shape)
    cent = centroids(labels)
    bundle_doc = write_bundle(dest / "bundle", affine, labels, cent, rng)
    lesion, disc = inputs(shape, affine)
    nib.save(nib.Nifti1Image(lesion, affine), str(dest / "lesion_mask.nii.gz"))
    nib.save(nib.Nifti1Image(disc, affine), str(dest / "fdisconnection_map.nii.gz"))
    (dest / "expected").mkdir(exist_ok=True)
    expected = expected_profile(labels, lesion, disc, bundle_doc, dest / "bundle", affine)
    (dest / "expected" / "cognet_profile.json").write_text(
        json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return expected


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent / "reference")
    contracts = Path(sys.argv[2] if len(sys.argv) > 2 else "contracts")
    exp = generate(out, contracts)
    print(f"reference written to {out}: {len(exp['concepts'])} concepts, {exp['method']['n_parcels']} parcels")
