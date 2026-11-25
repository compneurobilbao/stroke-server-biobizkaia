#!/usr/bin/env python3
"""Write the ``space-transform`` bundle ``xfm-nlin6asym-to-nlin2009aasym`` from the outputs of
``build.sh`` (which downloads the pinned sources and runs ``antsApplyTransforms``).

    python make_bundle.py --work WORK --out BUNDLE_DIR [--ants-version TEXT]

Reads, under WORK (written by build.sh):

    dl/...                          the pinned TemplateFlow files (checked by build.sh)
    ants/displacement.nii.gz        the TemplateFlow transform composed into one displacement
                                    field on the MNI152NLin2009aAsym 1 mm grid
                                    (antsApplyTransforms -o [field,1]; ITK convention: vectors
                                    in LPS millimetres, shape X, Y, Z, 1, 3)
    ants/nlin6asym_T1w_in_2009a.nii.gz
                                    the MNI152NLin6Asym T1w template resampled with the same
                                    transform by ANTs (linear), used only to check the conversion

Writes BUNDLE_DIR with ``coordinate_map.nii.gz`` (float32, X, Y, Z, 3: for each voxel of the
target grid, the RAS millimetre position in MNI152NLin6Asym of its centre, i.e. the
image-resampling direction), ``LICENSE`` (the template licence, byte for byte),
``README.md``, ``verification.json`` and ``bundle.yaml`` (files, sizes, SHA-256 and content
digest as in docs/contracts/bundles.md). Fails when the coordinate map does not reproduce
the ANTs resampling or does not improve the alignment of the two templates.

Only nibabel, numpy and PyYAML are needed. Nothing here is patient data: the inputs are the
public MNI152 templates and the published transform between them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml

BUNDLE_ID = "xfm-nlin6asym-to-nlin2009aasym"
BUNDLE_VERSION = "1.0.0"
FROM_SPACE, TO_SPACE = "MNI152NLin6Asym", "MNI152NLin2009aAsym"
TF = "https://templateflow.s3.amazonaws.com"
# The pinned sources (same list and checksums as build.sh).
SOURCES = {
    "xfm": (
        "tpl-MNI152NLin2009cAsym/tpl-MNI152NLin2009cAsym_from-MNI152NLin6Asym_mode-image_xfm.h5",
        "2e3869a07b96aec406e0419ca2e434afc54882d37cc212b933b139d1b63a4dfe",
    ),
    "licence": (
        "tpl-MNI152NLin2009cAsym/LICENSE",
        "46e280fe1a24c6f37feef2445ed288435e0ad063504a06030b206b12fdd2629d",
    ),
    "target_t1w": (
        "tpl-MNI152NLin2009aAsym/tpl-MNI152NLin2009aAsym_res-1_T1w.nii.gz",
        "a12fa7be3c02762e14ec770e940a46ce57dc6d7e4953c5228e4da646301b9fe1",
    ),
    "target_mask": (
        "tpl-MNI152NLin2009aAsym/tpl-MNI152NLin2009aAsym_res-1_desc-brain_mask.nii.gz",
        "17afe7c0696e788aeceb656e16bb5e907e8d22cf8871e088e31b31c9a1941b67",
    ),
    "source_t1w": (
        "tpl-MNI152NLin6Asym/tpl-MNI152NLin6Asym_res-01_T1w.nii.gz",
        "f675946c0a077e0682885c3d6085f10acb0dd22b2274a6cbbf04969c08a77258",
    ),
}
LPS_TO_RAS = np.array([-1.0, -1.0, 1.0])
CITATION = (
    "TemplateFlow: FAIR-sharing of multi-scale, multi-species brain models. Nature Methods 19, "
    "1568-1571 (2022). https://doi.org/10.1038/s41592-022-01681-2. Unbiased average "
    "age-appropriate atlases for pediatric studies. NeuroImage 54, 313-327 (2011). "
    "https://doi.org/10.1016/j.neuroimage.2010.07.033"
)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def voxel_centres(shape, affine) -> np.ndarray:
    ii, jj, kk = np.indices(shape[:3], dtype=np.float64)
    return np.stack([ii, jj, kk], -1) @ affine[:3, :3].T + affine[:3, 3]


def trilinear(img, coords: np.ndarray) -> np.ndarray:
    """Linear interpolation of ``img`` at RAS positions ``coords`` (0 outside)."""
    data = np.asarray(img.dataobj, dtype=np.float32)
    inv = np.linalg.inv(img.affine)
    v = coords.reshape(-1, 3) @ inv[:3, :3].T + inv[:3, 3]
    f = np.floor(v).astype(np.int64)
    t = (v - f).astype(np.float32)
    ok = np.all((f >= 0) & (f < np.array(data.shape) - 1), axis=1)
    f, t = f[ok], t[ok]
    acc = np.zeros(len(f), np.float32)
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                w = (t[:, 0] if dx else 1 - t[:, 0]) * (t[:, 1] if dy else 1 - t[:, 1])
                w = w * (t[:, 2] if dz else 1 - t[:, 2])
                acc += w * data[f[:, 0] + dx, f[:, 1] + dy, f[:, 2] + dz]
    out = np.zeros(len(v), np.float32)
    out[ok] = acc
    return out.reshape(coords.shape[:3])


def coordinate_map(field_img) -> tuple[np.ndarray, np.ndarray]:
    """RAS positions in the source space of every target voxel, from an ITK displacement
    field (LPS vectors) on the target grid."""
    disp = np.asarray(field_img.dataobj, dtype=np.float64)
    if disp.ndim == 5 and disp.shape[3] == 1:
        disp = disp[:, :, :, 0, :]
    if disp.ndim != 4 or disp.shape[3] != 3:
        raise SystemExit(f"unexpected displacement field shape {disp.shape}")
    centres = voxel_centres(disp.shape, field_img.affine)
    return (centres + disp * LPS_TO_RAS).astype(np.float32), centres


def corr(a, b, mask) -> float:
    return float(np.corrcoef(a[mask], b[mask])[0, 1])


def verify(work: Path, coords: np.ndarray, centres: np.ndarray) -> dict:
    dl = work / "dl"
    target = nib.load(str(dl / SOURCES["target_t1w"][0]))
    mask = np.asarray(nib.load(str(dl / SOURCES["target_mask"][0])).dataobj) > 0
    source = nib.load(str(dl / SOURCES["source_t1w"][0]))
    ants = np.asarray(nib.load(str(work / "ants" / "nlin6asym_T1w_in_2009a.nii.gz")).dataobj, np.float32)
    ours = trilinear(source, coords)
    none = trilinear(source, centres)
    ref = np.asarray(target.dataobj, dtype=np.float32)
    span = float(ants[mask].max() - ants[mask].min())
    disp = np.linalg.norm(coords - centres, axis=-1)[mask]
    result = {
        "coordinate_map_vs_ants_resampling": {
            "correlation_in_brain": round(corr(ours, ants, mask), 6),
            "max_abs_difference_over_range": round(float(np.abs(ours - ants)[mask].max()) / span, 6),
        },
        "t1w_correlation_with_target_in_brain": {
            "without_transform": round(corr(none, ref, mask), 4),
            "with_transform": round(corr(ours, ref, mask), 4),
        },
        "displacement_mm_in_brain": {
            "mean": round(float(disp.mean()), 3),
            "median": round(float(np.median(disp)), 3),
            "p95": round(float(np.percentile(disp, 95)), 3),
            "max": round(float(disp.max()), 3),
        },
    }
    if result["coordinate_map_vs_ants_resampling"]["correlation_in_brain"] < 0.9999:
        raise SystemExit("the coordinate map does not reproduce the ANTs resampling: " + json.dumps(result))
    tc = result["t1w_correlation_with_target_in_brain"]
    if tc["with_transform"] <= tc["without_transform"]:
        raise SystemExit("the transform does not improve the template alignment: " + json.dumps(result))
    return result


README = """# {id} {version}

Space transform from {frm} (the lesion space of the platform, FSL's MNI152 nonlinear 6th
generation template) to {to} (ICBM 2009a nonlinear asymmetric, the space of the HCP1065 tract
atlas), for the `lnm-tracts` step (1.1.0 and later), bundle type `space-transform`.

## Content

- `coordinate_map.nii.gz`: float32, shape {shape} x 3, on the {to} 1 mm grid of the TemplateFlow
  template `tpl-MNI152NLin2009aAsym_res-1_T1w.nii.gz` (affine in the header). Voxel (i, j, k)
  holds the RAS millimetre position in {frm} of its centre (image-resampling direction): an
  image of {frm} is brought to {to} by looking it up at these positions.
- `LICENSE`: licence of the templates, copied from the TemplateFlow archive.
- `verification.json`: checks run when the bundle was built.

## Source

The published TemplateFlow transform `{xfm}` (ANTs composite transform, SHA-256 {xfm_sha}),
composed into a displacement field on the target grid with `antsApplyTransforms` ({ants}) and
converted from ITK (LPS) to RAS coordinates by `scripts/bundles/space_transform/make_bundle.py`
of the stroke-server repository (`build.sh` in the same directory downloads every source at a
pinned URL and SHA-256 and rebuilds this bundle).

TemplateFlow publishes this transform for MNI152NLin2009cAsym; no {frm} to {to} transform is
published. The MNI ICBM 2009 atlas page describes the 2009a and 2009c templates as the same
anatomy with a different sampling and a different N3 version of the intensity correction, so the
2009c transform is used as the {frm} to {to} transform, applied on the 2009a 1 mm grid. This
build does not measure that equivalence itself (assumption; see the open points in
docs/reference-data.md, "The space-transform bundle").

## Licence

The ICBM 2009 templates are distributed by the MNI under the licence in `LICENSE` (use, copy,
modify and distribute for any purpose without fee, provided the copyright notice appears in all
copies). TemplateFlow distributes the transform inside its `tpl-MNI152NLin2009cAsym` archive,
whose description declares that licence file ("See LICENSE file"); it states no separate licence,
author or method for the transform itself. Keep `LICENSE` with any copy.

## Cite

{citation}
"""


def write_bundle(work: Path, out: Path, ants_version: str) -> dict:
    dl = work / "dl"
    for key, (rel, digest) in SOURCES.items():
        if sha256(dl / rel) != digest:
            raise SystemExit(f"{rel}: SHA-256 differs from the pinned value")
    field = nib.load(str(work / "ants" / "displacement.nii.gz"))
    target = nib.load(str(dl / SOURCES["target_t1w"][0]))
    if field.shape[:3] != target.shape or not np.allclose(field.affine, target.affine):
        raise SystemExit("the displacement field is not on the target template grid")
    coords, centres = coordinate_map(field)
    checks = verify(work, coords, centres)

    out.mkdir(parents=True, exist_ok=False)
    img = nib.Nifti1Image(coords, target.affine)
    img.header.set_xyzt_units("mm")
    img.set_sform(target.affine, code=4)  # MNI152 space
    img.set_qform(target.affine, code=4)
    img.header["descrip"] = f"RAS mm in {FROM_SPACE} of each {TO_SPACE} voxel"[:79].encode()
    nib.save(img, str(out / "coordinate_map.nii.gz"))
    (out / "LICENSE").write_bytes((dl / SOURCES["licence"][0]).read_bytes())
    (out / "verification.json").write_text(json.dumps(checks, indent=1) + "\n", encoding="utf-8")
    (out / "README.md").write_text(
        README.format(
            id=BUNDLE_ID,
            version=BUNDLE_VERSION,
            frm=FROM_SPACE,
            to=TO_SPACE,
            shape=" x ".join(str(n) for n in coords.shape[:3]),
            xfm=f"{TF}/{SOURCES['xfm'][0]}",
            xfm_sha=SOURCES["xfm"][1],
            ants=ants_version,
            citation=CITATION,
        ),
        encoding="utf-8",
    )
    files = []
    for p in sorted(out.iterdir(), key=lambda q: q.name.encode()):
        files.append({"path": p.name, "sha256": sha256(p), "size_bytes": p.stat().st_size})
    lines = "".join(f"{f['sha256']}  {f['path']}\n" for f in files)
    doc = {
        "contract_version": "1.0",
        "kind": "Bundle",
        "id": BUNDLE_ID,
        "version": BUNDLE_VERSION,
        "type": "space-transform",
        "title": f"Space transform {FROM_SPACE} to {TO_SPACE} (TemplateFlow, coordinate map)",
        "description": (
            f"Coordinate map on the {TO_SPACE} 1 mm grid giving, for each voxel, its RAS "
            f"millimetre position in {FROM_SPACE}; built from the TemplateFlow "
            "MNI152NLin6Asym to MNI152NLin2009cAsym transform (2009a and 2009c are treated as the same "
            "space; see README.md). Read by lnm-tracts 1.1.0 to resample lesions into the space "
            "of a tract atlas."
        ),
        "synthetic": False,
        "licence": {
            "spdx": "LicenseRef-MNI-ICBM152",
            "redistributable": True,
            "terms_url": f"{TF}/{SOURCES['licence'][0]}",
            "notes": (
                "MNI ICBM 2009 template licence (LICENSE in the bundle; keep it with any copy). "
                "The transform is distributed by TemplateFlow inside its tpl-MNI152NLin2009cAsym "
                "archive, whose description refers to that licence file; no separate licence, "
                "author or method is stated for the transform itself (open point, "
                "docs/reference-data.md)."
            ),
        },
        "citation": CITATION,
        "metadata": {
            "from_space": FROM_SPACE,
            "to_space": TO_SPACE,
            "grid_shape": list(coords.shape[:3]),
            "voxel_size_mm": 1.0,
            "direction": "image-resampling (target voxel -> source position)",
            "source_transform_url": f"{TF}/{SOURCES['xfm'][0]}",
            "source_transform_sha256": SOURCES["xfm"][1],
            "source_transform_space_pair": "MNI152NLin6Asym to MNI152NLin2009cAsym",
            "target_grid_url": f"{TF}/{SOURCES['target_t1w'][0]}",
            "target_grid_sha256": SOURCES["target_t1w"][1],
            "built_with": f"antsApplyTransforms {ants_version}; scripts/bundles/space_transform",
        },
        "content_digest": "sha256:" + hashlib.sha256(lines.encode("utf-8")).hexdigest(),
        "files": files,
    }
    (out / "bundle.yaml").write_text(
        yaml.safe_dump(doc, sort_keys=False, width=100), encoding="utf-8"
    )
    return {"bundle": str(out), "content_digest": doc["content_digest"], "checks": checks}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--ants-version", default="unknown")
    args = ap.parse_args()
    print(json.dumps(write_bundle(args.work, args.out, args.ants_version), indent=1))


if __name__ == "__main__":
    main()
