#!/usr/bin/env python3
"""Build the ``concept-maps`` bundle of the cognet-stroke step from the author's workflow outputs.

The author's workflow (``src/generate_cdr_report/cdr_report_workflow.py`` of COGNET-STROKE)
writes, per concept, an FDR-corrected concept z map and caches the thresholded null maps it
draws with brainsmash. This script turns those files into the bundle layout the step reads
(docs/reference-data.md, "The concept-maps bundle"); it computes nothing statistical itself:

* ``parcellation.nii.gz``: the author's parcellation (default
  ``src/generate_cdr_report/data/firsT_2566.nii.gz`` of the checkout), copied byte for byte. It
  must be on the 2 mm MNI152NLin6Asym working grid; its sorted non-zero labels are the parcels.
* ``concepts/<concept>.npz``: ``mask`` is the concept z map reduced to parcels and thresholded
  as the workflow does (``parcellater.fit_transform(img) > 2`` gives 1, otherwise -1), and
  ``nulls`` is the workflow's cache ``<concept>_nulls_thr_<parcellation name>.npy`` (already
  thresholded to 1 / -1), both as int8.
* ``concepts.tsv``: concept and domain, in the order of ``--concepts`` (default: the table next
  to this script, the author's 22 concepts with the provisional domain grouping of
  cognet-concept-maps 1.0.0; the author's workflow defines no domains).
* ``bundle.yaml``: files, SHA-256, content digest, licence and metadata.

The reduction to parcels uses neuromaps' ``Parcellater`` exactly as the workflow does, so run
the script in the author's analysis environment (``uv sync --frozen`` in the checkout). Without
neuromaps, ``--allow-numpy-parcellation`` uses a plain mean per parcel, which is the same
computation only when the concept maps are on the parcellation's grid (checked); the method used
is recorded in ``metadata.mask_parcellation``.

    python steps/cognet-stroke/bundle/build_bundle.py \\
        --upstream /path/to/COGNET-STROKE --maps-dir /path/to/concept_data_dir/corrected_fdr \\
        --version 1.1.0 --out /path/to/new/bundle

Then ``python contracts/validate.py OUT/bundle.yaml`` and ``scripts/bundles/registry.py register
OUT`` (docs/reference-data.md). No data is committed anywhere: the output directory is the
deployment's.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml

HERE = Path(__file__).resolve().parent
STEP_DIR = HERE.parent
REPO = STEP_DIR.parents[1]
sys.path.insert(0, str(STEP_DIR / "src"))

from cognet_stroke_step.bundle import (  # noqa: E402
    BundleLayoutError,
    read_concept_table,
    load_concept,
    load_concept_maps,
)
from cognet_stroke_step.parcels import parcel_ids  # noqa: E402

DEFAULT_PARCELLATION = Path("src") / "generate_cdr_report" / "data" / "firsT_2566.nii.gz"
MAP_NAME = "{concept}_z_desc-association_level-voxel_corr-FDR_method-indep.nii.gz"
NULL_NAME = "{concept}_nulls_thr_{parcellation}.npy"
THRESHOLD = 2  # z > 2 inside the concept, as the workflow
DEFAULT_NULL_METHOD = (
    "brainsmash Sampled ns=250 knn=500 pv=70 nh=50 on parcel-centroid distances, z > 2 "
    "(cache of the author's cdr_report_workflow)"
)


class BuildError(RuntimeError):
    pass


def working_grid() -> tuple[tuple[int, ...], np.ndarray]:
    types = yaml.safe_load((REPO / "contracts" / "artefact-types.yaml").read_text(encoding="utf-8"))
    grid = types["grids"]["MNI152NLin6Asym-res2"]
    return tuple(grid["shape"]), np.array(grid["affine"], dtype=float)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_digest(files: list[dict]) -> str:
    """bundles.md section 3 (the formula of contracts/validate.py ``bundle_digest``)."""
    lines = "".join(
        f"{f['sha256']}  {f['path']}\n" for f in sorted(files, key=lambda f: f["path"].encode())
    )
    return "sha256:" + hashlib.sha256(lines.encode("utf-8")).hexdigest()


def write_npz(path: Path, **arrays) -> None:
    """Deterministic .npz (fixed zip timestamps): the same inputs give the same bytes."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, arr in arrays.items():
            info = zipfile.ZipInfo(f"{name}.npy", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            with zf.open(info, "w") as fh:
                np.lib.format.write_array(fh, np.asarray(arr), allow_pickle=False)


def parcellation_name(path: Path) -> str:
    """The name the workflow gives its null cache: the file name up to the first dot."""
    return path.name.split(".")[0]


class NumpyParcellater:
    """Plain mean of the concept map over the voxels of every parcel; only on the same grid."""

    method = "numpy-mean-same-grid"

    def __init__(self, img: nib.Nifti1Image, ids: np.ndarray) -> None:
        self.img = img
        self.labels = np.rint(np.asanyarray(img.dataobj)).astype(np.int64)
        self.ids = ids

    def __call__(self, concept_img: nib.Nifti1Image) -> np.ndarray:
        if concept_img.shape[:3] != self.img.shape or not np.allclose(
            concept_img.affine, self.img.affine, rtol=0, atol=1e-3
        ):
            raise BuildError(
                "a concept map is not on the parcellation's grid; run in the author's analysis "
                "environment (neuromaps) instead of --allow-numpy-parcellation"
            )
        data = np.asanyarray(concept_img.dataobj).astype(np.float64).reshape(self.labels.shape)
        flat, lab = data.ravel(), self.labels.ravel()
        inside = np.isin(lab, self.ids)
        index = np.searchsorted(self.ids, lab[inside])
        sums = np.bincount(index, weights=flat[inside], minlength=self.ids.size)
        counts = np.bincount(index, minlength=self.ids.size)
        return sums / counts


class NeuromapsParcellater:
    """The workflow's reduction: neuromaps Parcellater(parcellation, space='MNI152',
    resampling_target='data').fit_transform(img, space='MNI152')."""

    method = "neuromaps-parcellater"

    def __init__(self, img: nib.Nifti1Image, ids: np.ndarray) -> None:
        from neuromaps import parcellate

        self.parcellater = parcellate.Parcellater(img, space="MNI152", resampling_target="data")
        self.ids = ids

    def __call__(self, concept_img: nib.Nifti1Image) -> np.ndarray:
        values = np.asarray(self.parcellater.fit_transform(concept_img, space="MNI152")).squeeze()
        if values.shape != (self.ids.size,):
            raise BuildError(
                f"the parcellater returned {values.shape} values for {self.ids.size} parcels"
            )
        return values.astype(np.float64)


def make_parcellater(img, ids, allow_numpy: bool):
    try:
        return NeuromapsParcellater(img, ids)
    except ImportError:
        if not allow_numpy:
            raise BuildError(
                "neuromaps is not importable: run in the author's analysis environment, or pass "
                "--allow-numpy-parcellation (concept maps on the parcellation's grid only)"
            ) from None
        return NumpyParcellater(img, ids)


def upstream_commit(upstream: Path | None) -> str | None:
    if upstream is None:
        return None
    marker = upstream / "UPSTREAM_COMMIT"
    if marker.is_file():
        return marker.read_text(encoding="utf-8").strip() or None
    try:
        out = subprocess.run(
            ["git", "-C", str(upstream), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def build(args: argparse.Namespace) -> dict:
    out: Path = args.out
    if out.exists() and any(out.iterdir()):
        raise BuildError(f"{out} exists and is not empty")
    parc_path = args.parcellation or (
        args.upstream / DEFAULT_PARCELLATION if args.upstream else None
    )
    if parc_path is None or not parc_path.is_file():
        raise BuildError("no parcellation: pass --parcellation or --upstream")
    parc_img = nib.load(str(parc_path))
    shape, affine = working_grid()
    if tuple(parc_img.shape) != shape or not np.allclose(parc_img.affine, affine, rtol=0, atol=1e-3):
        raise BuildError("the parcellation is not on the 2 mm MNI152NLin6Asym working grid")
    try:
        ids = parcel_ids(np.asanyarray(parc_img.dataobj))
    except ValueError as exc:
        raise BuildError(str(exc)) from exc
    if ids.size == 0:
        raise BuildError("the parcellation has no parcel")
    try:
        concepts = read_concept_table(args.concepts)
    except BundleLayoutError as exc:
        raise BuildError(f"{args.concepts}: {exc}") from exc

    pname = parcellation_name(parc_path)
    missing = [
        str(p)
        for c, _d in concepts
        for p in (
            args.maps_dir / MAP_NAME.format(concept=c),
            args.maps_dir / NULL_NAME.format(concept=c, parcellation=pname),
        )
        if not p.is_file()
    ]
    if missing:
        raise BuildError("missing workflow outputs:\n  " + "\n  ".join(missing))

    parcellater = make_parcellater(parc_img, ids, args.allow_numpy_parcellation)
    out.mkdir(parents=True, exist_ok=True)
    (out / "concepts").mkdir()
    shutil.copyfile(parc_path, out / "parcellation.nii.gz")
    (out / "concepts.tsv").write_text(
        "concept\tdomain\n" + "".join(f"{c}\t{d}\n" for c, d in concepts), encoding="utf-8"
    )
    n_nulls = None
    summary = []
    for concept, _domain in concepts:
        z = parcellater(nib.load(str(args.maps_dir / MAP_NAME.format(concept=concept))))
        mask = np.where(z > THRESHOLD, 1, -1).astype(np.int8)
        nulls = np.load(
            args.maps_dir / NULL_NAME.format(concept=concept, parcellation=pname), allow_pickle=False
        )
        if nulls.ndim != 2 or nulls.shape[1] != ids.size:
            raise BuildError(
                f"{concept}: null cache has shape {nulls.shape}, expected (n_nulls, {ids.size}); "
                "was it made with another parcellation?"
            )
        if not np.isin(nulls, (-1, 1)).all():
            raise BuildError(f"{concept}: null cache values must be 1 or -1 (thresholded nulls)")
        if n_nulls is None:
            n_nulls = int(nulls.shape[0])
        elif nulls.shape[0] != n_nulls:
            raise BuildError(f"{concept}: {nulls.shape[0]} null maps, the others have {n_nulls}")
        write_npz(out / "concepts" / f"{concept}.npz", mask=mask, nulls=nulls.astype(np.int8))
        summary.append((concept, int((mask == 1).sum())))

    files = [
        {"path": p.relative_to(out).as_posix(), "sha256": sha256(p), "size_bytes": p.stat().st_size}
        for p in sorted(out.rglob("*"))
        if p.is_file()
    ]
    metadata = {
        "n_concepts": len(concepts),
        "n_parcels": int(ids.size),
        "n_nulls": n_nulls,
        "parcellation": pname,
        "mask_threshold_z": THRESHOLD,
        "mask_parcellation": parcellater.method,
        "null_method": args.null_method,
        "domains_provisional": args.domains_provisional,
    }
    commit = upstream_commit(args.upstream)
    if commit:
        metadata["code_repository"] = "compneurobilbao/COGNET-STROKE"
        metadata["code_commit"] = commit
    licence = {"spdx": args.licence_spdx, "redistributable": args.redistributable}
    if args.licence_terms_url:
        licence["terms_url"] = args.licence_terms_url
    if args.licence_notes:
        licence["notes"] = args.licence_notes
    doc = {
        "contract_version": "1.2",
        "kind": "Bundle",
        "id": args.id,
        "version": args.version,
        "type": "concept-maps",
        "title": args.title or f"COGNET-STROKE concept maps and null masks ({pname}, {ids.size} parcels)",
        "description": (
            f"{len(concepts)} concepts as binary parcel masks on the {pname} parcellation "
            f"({ids.size} parcels), each with {n_nulls} null masks, built by "
            "steps/cognet-stroke/bundle/build_bundle.py from the concept maps and the null-map "
            "cache of the COGNET-STROKE workflow. Layout read by the cognet-stroke step."
        ),
        "synthetic": False,
        "licence": licence,
        "metadata": metadata,
        "content_digest": content_digest(files),
        "files": files,
    }
    if args.citation:
        doc["citation"] = args.citation
    (out / "bundle.yaml").write_text(yaml.safe_dump(doc, sort_keys=False, width=100), encoding="utf-8")

    # self-check with the step's own reader
    maps = load_concept_maps(out, shape, affine)
    for concept, _domain in maps.concepts:
        load_concept(maps, concept)
    for concept, inside in summary:
        print(f"{concept}: {inside} of {ids.size} parcels inside, {n_nulls} nulls")
    return doc


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--upstream", type=Path, help="checkout of COGNET-STROKE (default parcellation, commit)")
    ap.add_argument("--parcellation", type=Path, help="parcellation (default: the checkout's firsT_2566)")
    ap.add_argument("--maps-dir", type=Path, required=True,
                    help="the workflow's <concept_data_dir>/corrected_fdr (z maps and null caches)")
    ap.add_argument("--concepts", type=Path, default=HERE / "concepts.tsv",
                    help="concept and domain table (default: %(default)s)")
    ap.add_argument("--out", type=Path, required=True, help="new, empty bundle directory")
    ap.add_argument("--id", default="cognet-concept-maps")
    ap.add_argument("--version", required=True, help="bundle version (same layout, new content: a minor version)")
    ap.add_argument("--title")
    ap.add_argument("--citation")
    ap.add_argument("--null-method", default=DEFAULT_NULL_METHOD)
    ap.add_argument("--domains-provisional", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--licence-spdx", default="ODbL-1.0",
                    help="licence of the concept maps (default %(default)s: derived from Neurosynth)")
    ap.add_argument("--redistributable", action=argparse.BooleanOptionalAction, default=False)
    ap.add_argument("--licence-terms-url", default="https://github.com/neurosynth/neurosynth-data")
    ap.add_argument("--licence-notes")
    ap.add_argument("--allow-numpy-parcellation", action="store_true")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        doc = build(args)
    except BuildError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1
    print(f"written {args.out} ({doc['id']} {doc['version']}, {doc['content_digest']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
