#!/usr/bin/env python3
"""Build a ``slnm-coarse-cache`` bundle from the author's portable coarse cache.

The author's ``coarse_files`` directory (the ``--output-root`` of ``slnm-prepare --copy-inputs``,
or a legacy cache converted with ``slnm-materialize``) holds the common atlas and one directory
per participant. This script copies, for the participants of the given table only, exactly
the files ``slnm-coarse`` reads (never a tractogram, an inverse warp or an intermediate file),
checks what the step and ``slnm-coarse`` will check (portable copy mode, the participant name and
the atlas fingerprint in ``metadata.json``, the atlas on the 2 mm working grid with labels up to
2966, the four sites), writes ``participants.csv`` with the two columns the step needs, and
writes ``bundle.yaml`` (files, SHA-256, content digest, ``licence.redistributable: false``).
It computes nothing scientific and changes no file it copies. Register the result with
``python scripts/bundles/registry.py register DEST``.

    python steps/slnm-coarse/bundle/build_bundle.py \\
        --coarse-files /path/to/coarse_files --participants /path/to/participants.csv \\
        --dest /path/to/new/slnm-coarse-cache-hcp-aging --id slnm-coarse-cache-hcp-aging \\
        --version 1.0.0 --licence-spdx LicenseRef-HCP-Aging --terms-url https://...

Needs Python 3.10+, numpy, nibabel and PyYAML (any environment of this repository).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml

REPO = Path(__file__).resolve().parents[3]
ATLAS_FILENAME = "firsT_2566_MWMA400_min20.nii.gz"
N_LABELS = 2966
SITES = ("MGH", "UCLA", "UMinn", "WashU")
SUFFIX = "_DiffusionRecommended"
# What slnm-coarse reads per participant (REQUIRED of slnm_fast.coarse.cohort_coarse_lesion)
PARTICIPANT_FILES = (
    "metadata.json",
    "atlas_dwispace.nii.gz",
    "index/roi_offsets.npy",
    "index/track_ids.npy",
    "index/roi_weight_totals.npy",
    "dwi_bzero.nii.gz",
    "dwireg/standard2dwi0GenericAffine.mat",
    "dwireg/standard2dwi1Warp.nii.gz",
    "streamlines/sift2_weights.txt",
)
LAYOUT = "slnm-coarse-cache-v1"


class BuildError(Exception):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def working_grid() -> tuple[tuple[int, ...], np.ndarray]:
    registry = yaml.safe_load((REPO / "contracts" / "artefact-types.yaml").read_text(encoding="utf-8"))
    grid = registry["grids"]["MNI152NLin6Asym-res2"]
    return tuple(grid["shape"]), np.asarray(grid["affine"], dtype=float)


def check_atlas(path: Path) -> None:
    if not path.is_file():
        raise BuildError(f"no {ATLAS_FILENAME} in the coarse cache root")
    img = nib.load(str(path))
    shape, affine = working_grid()
    if tuple(img.shape) != shape or not np.allclose(img.affine, affine, rtol=0, atol=1e-4):
        raise BuildError("the atlas is not on the 2 mm MNI152NLin6Asym working grid (91 x 109 x 91)")
    data = np.asanyarray(img.dataobj)
    if not np.array_equal(data, np.rint(data)) or data.min() < 0 or data.max() != N_LABELS:
        raise BuildError(f"the atlas must have integer labels 0 to {N_LABELS}, with {N_LABELS} present")


def read_participants(path: Path) -> list[tuple[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not {"subject_label", "site"} <= set(reader.fieldnames or []):
            raise BuildError("the participant table needs the columns subject_label and site")
        rows = [(row["subject_label"].strip(), row["site"].strip()) for row in reader]
    labels = [label for label, _ in rows]
    if not rows or len(set(labels)) != len(labels):
        raise BuildError("the participant table is empty or has duplicate subject labels")
    bad = [label for label, site in rows if site not in SITES or label.endswith(SUFFIX)]
    if bad:
        raise BuildError(
            f"{len(bad)} rows have a site other than {', '.join(SITES)} or a label with "
            f"{SUFFIX} (use the label without it)"
        )
    counts = {s: sum(1 for _, site in rows if site == s) for s in SITES}
    if len(rows) <= len(SITES) or not all(counts.values()):
        raise BuildError("every site must be present and the cohort must have more than four participants")
    return rows


def check_participant(source: Path, name: str, atlas_sha: str) -> None:
    for rel in PARTICIPANT_FILES:
        path = source / rel
        if not path.is_file():
            raise BuildError(f"{name}: missing {rel}")
        if path.is_symlink():
            raise BuildError(
                f"{name}: {rel} is a symbolic link: the cache is not portable (build it with "
                "slnm-prepare --copy-inputs or convert it with slnm-materialize)"
            )
    meta = json.loads((source / "metadata.json").read_text(encoding="utf-8"))
    if meta.get("participant") != name:
        raise BuildError(f"{name}: metadata.json names another participant")
    if meta.get("inputs_mode") != "copy":
        raise BuildError(f"{name}: inputs_mode is not copy: the cache is not portable")
    if meta.get("input_fingerprints", {}).get("mni_atlas", {}).get("sha256") != atlas_sha:
        raise BuildError(f"{name}: the cache was built with another atlas")


def build(args: argparse.Namespace) -> dict:
    source, dest = args.coarse_files.resolve(), args.dest.resolve()
    if dest.exists() and any(dest.iterdir()):
        raise BuildError(f"{dest} exists and is not empty")
    atlas = source / ATLAS_FILENAME
    check_atlas(atlas)
    atlas_sha = sha256(atlas)
    rows = read_participants(args.participants)
    for label, _ in rows:
        check_participant(source / f"{label}{SUFFIX}", f"{label}{SUFFIX}", atlas_sha)

    copy = shutil.copyfile if not args.hardlink else lambda a, b: Path(b).hardlink_to(a)
    (dest / "cache").mkdir(parents=True, exist_ok=True)
    copy(atlas, dest / "cache" / ATLAS_FILENAME)
    for label, _ in rows:
        for rel in PARTICIPANT_FILES:
            target = dest / "cache" / f"{label}{SUFFIX}" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            copy(source / f"{label}{SUFFIX}" / rel, target)
    with (dest / "participants.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(["subject_label", "site"])
        writer.writerows(rows)

    rels = sorted(
        (p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file() and p.name != "bundle.yaml"),
        key=lambda rel: rel.encode(),
    )
    files = [{"path": rel, "sha256": sha256(dest / rel), "size_bytes": (dest / rel).stat().st_size} for rel in rels]
    digest = hashlib.sha256("".join(f"{f['sha256']}  {f['path']}\n" for f in files).encode()).hexdigest()
    counts = {s: sum(1 for _, site in rows if site == s) for s in SITES}
    licence = {"spdx": args.licence_spdx, "redistributable": False}
    if args.terms_url:
        licence["terms_url"] = args.terms_url
    licence["notes"] = (
        "Derived from the diffusion data of a normative cohort under its data use terms: never "
        "committed, never in an image, a release tarball or an installer; installed locally."
    )
    doc = {
        "contract_version": "1.2",
        "kind": "Bundle",
        "id": args.id,
        "version": args.version,
        "type": "slnm-coarse-cache",
        "title": args.title,
        "description": (
            "Portable per-participant cache of slnm-coarse (native atlas, ROI-to-streamline index, "
            "B0, forward ANTs transforms, SIFT2 weights; no tractograms) and the participant table "
            "of the normative cohort, for the slnm-coarse step."
        ),
        "synthetic": False,
        "licence": licence,
        "metadata": {
            "layout": LAYOUT,
            "atlas": f"cache/{ATLAS_FILENAME}",
            "n_participants": len(rows),
            **{f"n_participants_{s.lower()}": n for s, n in counts.items()},
        },
        "content_digest": "sha256:" + digest,
        "files": files,
    }
    if args.citation:
        doc["citation"] = args.citation
    (dest / "bundle.yaml").write_bytes(yaml.safe_dump(doc, sort_keys=False, width=100).encode("utf-8"))
    return {"participants": len(rows), "files": len(files), **counts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--coarse-files", type=Path, required=True, help="the author's portable cache root")
    parser.add_argument("--participants", type=Path, required=True, help="CSV with subject_label and site")
    parser.add_argument("--dest", type=Path, required=True, help="new (empty) bundle directory")
    parser.add_argument("--id", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--title", default="Normative cohort cache for slnm-coarse")
    parser.add_argument("--licence-spdx", required=True, help="LicenseRef-<name> of the cohort terms")
    parser.add_argument("--terms-url")
    parser.add_argument("--citation")
    parser.add_argument("--hardlink", action="store_true", help="hard-link instead of copying (same file system)")
    args = parser.parse_args(argv)
    try:
        print(json.dumps(build(args)))
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
