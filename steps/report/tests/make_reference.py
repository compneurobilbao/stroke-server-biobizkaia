"""Synthetic reference inputs of the research-report step (nothing generated is committed).

* lesion mask: a synthetic mask copied from fixtures/data/mni_masks;
* lesion_qc.json: computed from that mask with numpy (same fields as the lesion-qc step);
* tract table, functional disconnection map, COGNET profile: invented values;
* run provenance: tests/data/run_provenance.json (committed, fake digests).

Every byte is deterministic: gzip files use stored blocks so that the zlib build does not
matter, and nothing depends on the clock.

    python steps/report/tests/make_reference.py [DEST_DIR]
"""

from __future__ import annotations

import json
import shutil
import struct
import sys
import zlib
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml
from scipy import ndimage

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
MASK_SOURCE = REPO / "fixtures" / "data" / "mni_masks" / "lesion_left_medium.nii.gz"
RUN_PROVENANCE = HERE / "data" / "run_provenance.json"

TRACTS = [
    ("toy_arcuate_left", "0.4375"),
    ("toy_cc_body", "0.0625"),
    ("toy_cst_left", "0.8125"),
    ("toy_cst_right", "0.0"),
]


def working_grid() -> tuple[tuple, np.ndarray]:
    registry = yaml.safe_load(
        (REPO / "contracts" / "artefact-types.yaml").read_text(encoding="utf-8")
    )
    grid = registry["grids"]["MNI152NLin6Asym-res2"]
    return tuple(grid["shape"]), np.array(grid["affine"], dtype=float)


def _write_text(path: Path, text: str) -> None:
    """Write UTF-8 with LF line ends on every platform (the bytes are hashed)."""
    path.write_bytes(text.encode("utf-8"))


def _r(x, n=4):
    return round(float(x), n)


def lesion_qc_for(mask: np.ndarray, affine: np.ndarray) -> dict:
    """lesion_qc document for a mask on the working grid (independent of the lesion-qc step)."""
    idx = np.argwhere(mask)
    world = idx @ affine[:3, :3].T + affine[:3, 3]
    n = len(idx)
    vol = n * 8.0 / 1000.0
    left = int((world[:, 0] < -1).sum())
    right = int((world[:, 0] > 1).sum())
    labels, count = ndimage.label(mask, structure=np.ones((3, 3, 3), dtype=bool))
    sizes = np.sort(np.bincount(labels.ravel())[1:])[::-1]
    return {
        "schema_version": "1.0",
        "mode": "mni_mask",
        "input_space": "MNI152NLin6Asym",
        "grid": {
            "name": "MNI152NLin6Asym-res2",
            "shape": list(mask.shape),
            "voxel_size_mm": [2.0, 2.0, 2.0],
            "source_shape": list(mask.shape),
            "source_voxel_size_mm": [2.0, 2.0, 2.0],
            "resampled": False,
        },
        "n_voxels": n,
        "volume_ml": _r(vol),
        "volume_source_ml": _r(vol),
        "hemisphere": {
            "label": "left" if left > right else "right",
            "left_voxels": left,
            "right_voxels": right,
            "midline_voxels": n - left - right,
        },
        "components": {
            "count": int(count),
            "connectivity": 26,
            "volumes_ml": [_r(s * 8.0 / 1000.0) for s in sizes[:20]],
            "largest_fraction": _r(sizes[0] / sizes.sum()),
        },
        "bounding_box": {
            "voxel": {"min": idx.min(axis=0).tolist(), "max": idx.max(axis=0).tolist()},
            "world_mm": {
                "min": [_r(v, 2) for v in world.min(axis=0)],
                "max": [_r(v, 2) for v in world.max(axis=0)],
            },
        },
        "centroid_mm": [_r(v, 2) for v in world.mean(axis=0)],
        "warnings": [],
        "thresholds": {
            "warn_below_ml": 0.5,
            "max_volume_ml": 800.0,
            "reject_bilateral": False,
            "bilateral_min_fraction": 0.05,
        },
    }


def gzip_stored(payload: bytes) -> bytes:
    """gzip container with stored (uncompressed) deflate blocks and a fixed header.

    gzip.compress writes the OS byte and (before 3.12) the header differently across
    platforms and Python versions; the bytes of this file are hashed, so build it by hand.
    """
    header = bytes([0x1F, 0x8B, 0x08, 0, 0, 0, 0, 0, 0, 0xFF])  # fixed: no name, mtime 0, OS unknown
    co = zlib.compressobj(0, zlib.DEFLATED, -15)
    body = co.compress(payload) + co.flush()
    return header + body + struct.pack("<II", zlib.crc32(payload) & 0xFFFFFFFF, len(payload))


def write_nifti_gz(path: Path, data: np.ndarray, affine: np.ndarray) -> None:
    img = nib.Nifti1Image(data, affine)
    img.set_qform(affine, code=4)
    img.set_sform(affine, code=4)
    path.write_bytes(gzip_stored(img.to_bytes()))


def disconnection_map(mask: np.ndarray, affine: np.ndarray) -> np.ndarray:
    """A float32 map that falls off linearly with the distance from the lesion centroid."""
    # integer sums divided once: the same float64 on every platform and numpy build
    idx = np.argwhere(mask)
    centre = idx.sum(axis=0) / len(idx)
    ii, jj, kk = np.indices(mask.shape)
    dist = np.sqrt((ii - centre[0]) ** 2 + (jj - centre[1]) ** 2 + (kk - centre[2]) ** 2)
    return np.maximum(0.0, 1.0 - dist / 14.0).astype(np.float32)


def _result(score, p, pc, sig):
    return {"score": score, "p_value": p, "p_corrected": pc, "significant": sig}


def cognet_profile() -> dict:
    concepts = [
        ("naming", "language", (1.75, 0.0125, 0.0375, True), (0.5, 0.4, 1.0, False), (2.0, 0.005, 0.015, True)),
        ("reading", "language", (1.25, 0.05, 0.15, False), (0.25, 0.7, 1.0, False), (1.5, 0.02, 0.06, False)),
        ("attention", "executive", (0.75, 0.3, 0.9, False), (0.5, 0.45, 1.0, False), (0.25, 0.6, 1.0, False)),
    ]
    out = []
    for name, domain, pos, neg, ovl in concepts:
        out.append(
            {
                "concept": name,
                "domain": domain,
                "disconnection_positive": _result(*pos),
                "disconnection_negative": _result(*neg),
                "lesion_overlap": _result(*ovl),
            }
        )
    domains = [
        {
            "domain": "language",
            "n_concepts": 2,
            "disconnection_positive": _result(1.5, 0.025, 0.05, True),
            "disconnection_negative": _result(0.375, 0.55, 1.0, False),
            "lesion_overlap": _result(1.75, 0.01, 0.02, True),
        },
        {
            "domain": "executive",
            "n_concepts": 1,
            "disconnection_positive": _result(0.75, 0.3, 0.6, False),
            "disconnection_negative": _result(0.5, 0.45, 0.9, False),
            "lesion_overlap": _result(0.25, None, None, False),
        },
    ]
    return {
        "kind": "CognetProfile",
        "profile_version": "1.0",
        "method": {
            "name": "concept-to-disconnection-representation",
            "alpha": 0.05,
            "correction": "bonferroni",
            "n_nulls": 1000,
            "n_parcels": 100,
            "measures": ["disconnection_positive", "disconnection_negative", "lesion_overlap"],
        },
        "bundle": {
            "id": "toy-concept-maps",
            "version": "1.0.0",
            "content_digest": "sha256:" + "ab12" * 16,
            "synthetic": True,
        },
        "concepts": out,
        "domains": domains,
    }


def generate(dest: Path) -> dict:
    """Write the reference inputs under ``dest`` and return their paths by port name."""
    dest.mkdir(parents=True, exist_ok=True)
    _, affine = working_grid()
    shutil.copyfile(MASK_SOURCE, dest / "lesion_mask.nii.gz")
    mask = np.asanyarray(nib.load(str(MASK_SOURCE)).dataobj) > 0
    _write_text(
        dest / "lesion_qc.json",
        json.dumps(lesion_qc_for(mask, affine), indent=2, sort_keys=True) + "\n",
    )
    write_nifti_gz(dest / "fdisconnection_map.nii.gz", disconnection_map(mask, affine), affine)
    _write_text(
        dest / "tract_disconnection.tsv",
        "tract\tdisconnection\n" + "".join(f"{t}\t{v}\n" for t, v in TRACTS),
    )
    _write_text(
        dest / "cognet_profile.json",
        json.dumps(cognet_profile(), indent=2, sort_keys=True) + "\n",
    )
    # a checkout with CRLF conversion must not change the digest
    (dest / "run_provenance.json").write_bytes(
        RUN_PROVENANCE.read_bytes().replace(b"\r\n", b"\n")
    )
    return {
        "lesion_qc": dest / "lesion_qc.json",
        "lesion_mask": dest / "lesion_mask.nii.gz",
        "run_provenance": dest / "run_provenance.json",
        "fdisconnection_map": dest / "fdisconnection_map.nii.gz",
        "tract_disconnection": dest / "tract_disconnection.tsv",
        "cognet_profile": dest / "cognet_profile.json",
    }


def main() -> None:
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "reference"
    paths = generate(dest)
    print("wrote " + ", ".join(sorted(p.name for p in paths.values())))


if __name__ == "__main__":
    main()
