"""Parcel-level structural disconnection: contract wrapper around ``slnm-coarse``.

The coarse workflow of the author's fast-slnm package (``slnm_fast.coarse.cohort_coarse_lesion``,
installed unmodified at the commit of ``upstream.env``) is run as a subprocess, once per run,
with the image's Python. This module maps the contract (``/in``, ``/ref``, parameters,
``/out``) onto that command and its output files; it computes no score itself.

Mapping:

* input ``lesion_mask`` (working grid) becomes ``<tmp>/lesion.nii.gz``: the same voxels, as
  uint8, with the header of the cache atlas, whose grid is checked to be the working grid
  (``slnm-coarse`` requires the lesion to have exactly the atlas shape and affine; nothing is
  resampled);
* bundle ``cohort_cache`` supplies ``--cache-root`` (``cache/``) and ``--participants``
  (``participants.csv``);
* parameter ``workers`` becomes ``--workers`` (at most the CPUs granted to the step);
* ``lesion_slnm.nii.gz`` and ``valid_mask.nii.gz`` become the outputs ``sdisconnection_map``
  and ``sdisconnection_valid_mask``; in the map, parcels without a valid group estimate are
  NaN instead of the zero ``slnm-coarse`` writes there (the atlas background stays zero).

``support_count.nii.gz``, ``roi_statistics.csv`` and the run summary (without paths) are kept
in ``/out/extra``; the participant-level files (participant identifiers, scores and
checkpoints of the normative cohort) stay in ``/tmp`` and are discarded, and only aggregate
counts of the participant QC reach the provenance warnings and the summary.
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
from collections import Counter
from importlib import metadata
from pathlib import Path

import nibabel as nib
import numpy as np

from stroke_step_sdk import InputRejected, InternalFailure, StepContext, contracts

# The author's code (module run with ``python -m``; equal to the ``slnm-coarse`` console script)
UPSTREAM_MODULE = "slnm_fast.coarse.cohort_coarse_lesion"
UPSTREAM_COMMIT_FILE = Path("/opt/upstream/fast-slnm/UPSTREAM_COMMIT")
ANTS = "antsApplyTransforms"

# Constants of slnm_fast.cohort_inputs at the pinned commit (a test keeps them equal)
ATLAS_FILENAME = "firsT_2566_MWMA400_min20.nii.gz"
N_LABELS = 2966
SITES = ("MGH", "UCLA", "UMinn", "WashU")
CACHE_SUFFIX = "_DiffusionRecommended"

# The slnm-coarse-cache bundle
BUNDLE = "cohort_cache"
CACHE_DIR = "cache"
PARTICIPANTS_FILE = "participants.csv"

GRID = "MNI152NLin6Asym-res2"
LESION_NAME = "lesion"  # slnm-coarse names its map <lesion name>_slnm.nii.gz
PATHS = re.compile(r"(?:/[\w.@+=,~-]+)+/?")
PARTICIPANT_IDS = re.compile(r"HCA\w*")


# --- checks of the bundle and the lesion ------------------------------------------------------


def working_grid() -> tuple[tuple[int, ...], np.ndarray]:
    grid = contracts.registry()["grids"][GRID]
    return tuple(grid["shape"]), np.asarray(grid["affine"], dtype=float)


def _layout_error(message: str, **details) -> InternalFailure:
    return InternalFailure("bundle_layout_unsupported", message, details=details or None)


def load_atlas(cache: Path) -> tuple[nib.Nifti1Image, np.ndarray]:
    """The common atlas of the cache root: on the working grid, integer labels 0..2966 with
    2966 present (what slnm-coarse requires)."""
    path = cache / ATLAS_FILENAME
    if not path.is_file():
        raise _layout_error(f"The cohort cache has no {CACHE_DIR}/{ATLAS_FILENAME}.")
    img = nib.Nifti1Image.from_filename(str(path))
    shape, affine = working_grid()
    if tuple(img.shape) != shape or not np.allclose(img.affine, affine, rtol=0, atol=1e-4):
        raise _layout_error("The atlas of the cohort cache is not on the working grid.")
    data = np.asanyarray(img.dataobj)
    labels = data.astype(np.int64)
    if not np.array_equal(labels, data) or labels.min() < 0 or labels.max() != N_LABELS:
        raise _layout_error(
            f"The atlas of the cohort cache must have integer labels 0 to {N_LABELS}, "
            f"with {N_LABELS} present."
        )
    return img, labels.astype(np.int32)


def read_participants(path: Path) -> list[tuple[str, str]]:
    """``(subject_label, site)`` rows of the bundle's participant table (the author's code
    validates the labels again)."""
    if not path.is_file():
        raise _layout_error(f"The cohort cache bundle has no {PARTICIPANTS_FILE}.")
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not {"subject_label", "site"} <= set(reader.fieldnames or []):
            raise _layout_error(f"{PARTICIPANTS_FILE} needs the columns subject_label and site.")
        rows = [(row["subject_label"], row["site"]) for row in reader]
    unknown = sum(1 for _, site in rows if site not in SITES)
    if unknown:
        raise _layout_error(
            f"{PARTICIPANTS_FILE} has sites other than {', '.join(SITES)}.", rows=unknown
        )
    return rows


def check_cohort(cache: Path, rows: list[tuple[str, str]]) -> dict[str, int]:
    """Every participant has a cache directory, and the cohort can give valid parcels at all:
    every site present and more than four participants (the author's validity rule)."""
    missing = sum(1 for label, _ in rows if not (cache / f"{label}{CACHE_SUFFIX}").is_dir())
    if missing:
        raise _layout_error(
            "Participants of the table have no directory in the cache.",
            missing_participant_caches=missing,
        )
    sites = Counter(site for _, site in rows)
    if len(rows) <= len(SITES) or any(sites[s] == 0 for s in SITES):
        raise InternalFailure(
            "cohort_insufficient",
            "The cohort cannot give a valid parcel: every site must be present and the cohort "
            f"must have more than {len(SITES)} participants.",
            details={"participants": len(rows), **{f"site_{s}": sites[s] for s in SITES}},
        )
    return {s: sites[s] for s in SITES}


def lesion_on_atlas(
    lesion_path: Path, atlas_img: nib.Nifti1Image, atlas: np.ndarray, dest: Path
) -> int:
    """Writes the lesion (working grid, checked by the SDK) with the atlas header, so that its
    shape and affine are exactly those of the atlas; returns its voxel count."""
    lesion = np.asanyarray(nib.load(str(lesion_path)).dataobj) > 0
    if not lesion.any():
        raise InputRejected("mask_empty", "The lesion mask has no voxels set.", port="lesion_mask")
    if not (lesion & (atlas > 0)).any():
        raise InputRejected(
            "lesion_outside_atlas",
            "The lesion mask has no voxel in any parcel of the cohort cache atlas.",
            port="lesion_mask",
        )
    header = atlas_img.header.copy()
    header.set_data_dtype(np.uint8)
    header["scl_slope"], header["scl_inter"] = 1, 0
    write_nifti(dest, nib.Nifti1Image(lesion.astype(np.uint8), atlas_img.affine, header))
    return int(lesion.sum())


# --- running the author's code -----------------------------------------------------------------


def scrub(text: str) -> str:
    """Text of the author's code for ``details``: paths and participant identifiers removed,
    whitespace collapsed, truncated."""
    text = PARTICIPANT_IDS.sub("<participant>", PATHS.sub("<path>", text))
    return " ".join(text.split())[:300]


def _last_line(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def run_slnm_coarse(
    cache: Path, participants: Path, lesion: Path, output: Path, workers: int, log
) -> None:
    cmd = [
        sys.executable, "-m", UPSTREAM_MODULE,
        "--cache-root", str(cache),
        "--participants", str(participants),
        "--lesion", str(lesion),
        "--output", str(output),
        "--workers", str(workers),
    ]  # fmt: skip
    env = {**os.environ, "OMP_NUM_THREADS": "1", "ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS": "1"}
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", env=env, check=False
    )
    for line in proc.stdout.splitlines():
        if line.strip():
            log.info("slnm-coarse: %s", scrub(line))
    if proc.returncode != 0:
        raise InternalFailure(
            "slnm_coarse_failed",
            "slnm-coarse stopped with an error.",
            details={"exit_code": proc.returncode, "error": scrub(_last_line(proc.stderr))},
        )


# --- outputs -------------------------------------------------------------------------------------


def write_nifti(path: Path, img: nib.Nifti1Image) -> None:
    """Gzip NIfTI with a fixed gzip header (no modification time, no file name), so that the
    same result gives the same bytes under every runtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=6) as gz:
            gz.write(img.to_bytes())


def _image(path: Path, shape: tuple[int, ...]) -> np.ndarray:
    if not path.is_file():
        raise InternalFailure("slnm_coarse_output_missing", f"slnm-coarse did not write {path.name}.")
    data = np.asanyarray(nib.load(str(path)).dataobj)
    if tuple(data.shape) != shape:
        raise InternalFailure("slnm_coarse_output_invalid", f"{path.name} is not on the atlas grid.")
    return data


def group_outputs(
    out_dir: Path, atlas: np.ndarray
) -> tuple[np.ndarray, np.ndarray, dict[str, int]]:
    """The group map with NaN on atlas voxels without a valid estimate, the validity mask
    (uint8), and parcel counts."""
    final = _image(out_dir / f"{LESION_NAME}_slnm.nii.gz", atlas.shape).astype(np.float32)
    valid_raw = _image(out_dir / "valid_mask.nii.gz", atlas.shape)
    if not np.isin(valid_raw, (0, 1)).all():
        raise InternalFailure("slnm_coarse_output_invalid", "valid_mask is not binary.")
    valid = (valid_raw == 1) & (atlas > 0)
    if not np.isfinite(final).all() or final.min() < 0 or final.max() > 1:
        raise InternalFailure(
            "slnm_coarse_output_invalid", "The slnm-coarse map has values outside [0, 1]."
        )
    if np.any(final[~valid] != 0):
        raise InternalFailure(
            "slnm_coarse_output_invalid", "The slnm-coarse map is non-zero outside its valid mask."
        )
    result = final.copy()
    result[(atlas > 0) & ~valid] = np.nan
    parcels = np.unique(atlas[atlas > 0])
    valid_parcels = np.unique(atlas[valid])
    counts = {"atlas_parcels": int(parcels.size), "valid_parcels": int(valid_parcels.size)}
    return result, valid.astype(np.uint8), counts


def participant_qc(path: Path) -> dict[str, float | int]:
    """Aggregates of the participant QC table (no identifiers)."""
    if not path.is_file():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))

    def column(name: str) -> np.ndarray:
        return np.array([float(r[name]) for r in rows if r.get(name, "") != ""], dtype=float)

    native, outside = column("lesion_native_voxels"), column("lesion_outside_atlas")
    seconds = column("subject_seconds")
    fraction = np.divide(outside, native, out=np.zeros_like(outside), where=native > 0)
    return {
        "participants": len(rows),
        "lesion_lost_in_native_space": int(np.sum(native == 0)),
        "lesion_partly_outside_native_atlas": int(np.sum(outside > 0)),
        "median_fraction_outside_native_atlas": float(np.median(fraction)) if fraction.size else 0.0,
        "median_participant_seconds": float(np.median(seconds)) if seconds.size else 0.0,
    }


def _record_software(ctx: StepContext) -> None:
    if UPSTREAM_COMMIT_FILE.is_file():
        ctx.add_software("fast-slnm (slnm-coarse)", UPSTREAM_COMMIT_FILE.read_text().strip())
    try:
        first = subprocess.run(
            [ANTS, "--version"], capture_output=True, text=True, check=False
        ).stdout.splitlines()
        version = next((line.split(":", 1)[1].strip() for line in first if ":" in line), "")
        if version:
            ctx.add_software("ANTs antsApplyTransforms", version[:64])
    except OSError:
        pass
    for package in ("slnm-fast", "numpy", "nibabel"):
        try:
            ctx.add_software(package, metadata.version(package))
        except metadata.PackageNotFoundError:
            pass


def run(ctx: StepContext) -> None:
    bundle = ctx.bundle_path(BUNDLE)
    cache = bundle / CACHE_DIR
    atlas_img, atlas = load_atlas(cache)
    rows = read_participants(bundle / PARTICIPANTS_FILE)
    sites = check_cohort(cache, rows)

    work = ctx.tmp_dir / "slnm-coarse"
    work.mkdir()
    lesion = work / f"{LESION_NAME}.nii.gz"
    voxels = lesion_on_atlas(ctx.input_path("lesion_mask"), atlas_img, atlas, lesion)

    workers = min(int(ctx.params["workers"]), max(int(ctx.cpus), 1))
    ctx.log.info(
        "slnm-coarse on %d participants (%s), lesion of %d voxels, %d worker(s)",
        len(rows),
        ", ".join(f"{s} {n}" for s, n in sites.items()),
        voxels,
        workers,
    )
    output = work / "output"
    run_slnm_coarse(cache, bundle / PARTICIPANTS_FILE, lesion, output, workers, ctx.log)

    result, valid, counts = group_outputs(output, atlas)
    shape_header = atlas_img.header.copy()
    shape_header["scl_slope"], shape_header["scl_inter"] = 1, 0
    map_header = shape_header.copy()
    map_header.set_data_dtype(np.float32)
    write_nifti(
        ctx.output_path("sdisconnection_map"),
        nib.Nifti1Image(result, atlas_img.affine, map_header),
    )
    mask_header = shape_header.copy()
    mask_header.set_data_dtype(np.uint8)
    write_nifti(
        ctx.output_path("sdisconnection_valid_mask"),
        nib.Nifti1Image(valid, atlas_img.affine, mask_header),
    )

    unsupported = counts["atlas_parcels"] - counts["valid_parcels"]
    if unsupported:
        ctx.warn(
            "unsupported_parcels",
            f"{unsupported} of {counts['atlas_parcels']} atlas parcels have no valid group "
            "estimate (a site without supporting participants, or at most four in total); they "
            "are NaN in the map and 0 in the validity mask, not zero disconnection.",
            port="sdisconnection_map",
        )
    qc = participant_qc(output / "participant_qc.csv")
    if qc.get("lesion_lost_in_native_space"):
        ctx.warn(
            "lesion_lost_in_native_space",
            f"For {qc['lesion_lost_in_native_space']} of {qc['participants']} participants the "
            "lesion has no voxel after the nearest-neighbour warp to native diffusion space; "
            "they contribute zero scores.",
            port="sdisconnection_map",
        )
    if qc.get("lesion_partly_outside_native_atlas"):
        ctx.warn(
            "lesion_outside_native_atlas",
            f"For {qc['lesion_partly_outside_native_atlas']} of {qc['participants']} participants "
            "part of the warped lesion lies outside the native atlas (median fraction "
            f"{qc['median_fraction_outside_native_atlas']:.3f}); that part is not modelled.",
            port="sdisconnection_map",
        )

    summary = {}
    if (output / "summary.json").is_file():
        summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
        summary.pop("lesion", None)  # an absolute path inside the container
    summary.update({"lesion_voxels": voxels, **counts, "participant_qc": qc})
    ctx.extra_path("slnm_coarse_summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    for name in ("support_count.nii.gz", "roi_statistics.csv"):
        if (output / name).is_file():
            shutil.copyfile(output / name, ctx.extra_path(name))
    _record_software(ctx)
