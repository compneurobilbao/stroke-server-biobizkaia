"""Functional disconnection: contract wrapper around the packaged flnm-fast engine.

The engine (``/opt/lesion_zmaps/create_lesion_zmaps.py`` of the base image
``compneurobilbaolab/flnm-fast``) is run unchanged, as a subprocess, on one participant:
this module only maps the contract (``/in``, ``/ref``, parameters, ``/out``) onto the
engine's command line and its output files, and the engine's per-participant failures onto
declared error codes. It computes nothing itself.

Mapping:

* input ``lesion_mask`` becomes ``<tmp>/lesion/case.nii.gz``;
* ``<tmp>/participants.tsv`` has one row, the identifier of the case: the engine is run without
  ``--match-age-sex``, so it uses every participant of the normative cohort (the bundle is the
  normative choice) and needs neither the age and sex of the case nor ``--control-count``;
* bundle ``normative`` supplies ``--fc``, ``--normative-subjects`` and ``--parcellation``; the
  parcellation file is named by the bundle (see ``parcellation_path``);
* ``<tmp>/zmaps/case_zmap.nii.gz`` becomes output ``fdisconnection_map``.

The engine's other outputs (control table with the participant identifiers of the normative
cohort, overlap table, per-parcel table) stay in ``/tmp`` and are not published.
"""

from __future__ import annotations

import csv
import hashlib
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import nibabel as nib
import numpy as np
import yaml

from stroke_step_sdk import InputRejected, InternalFailure, StepContext, contracts

ENGINE_SCRIPT = Path("/opt/lesion_zmaps/create_lesion_zmaps.py")
BUNDLE = "normative"
FC_FILE = "fc_row_subject_col.npy"
SUBJECTS_FILE = "normative_subjects.csv"
BUNDLE_MANIFEST = "bundle.yaml"
PARCELLATION_DIR = "standard"
PARCELLATION_SUFFIX = ".nii.gz"
GRID = "MNI152NLin6Asym-res2"
PARTICIPANT_ID = "case"
PATHS = re.compile(r"(?:/[\w.@+=,~-]+)+/?")

# Engine failure texts (str of the exception, written to failures.csv) -> declared code.
FAILURES = (
    (re.compile(r"^Empty lesion mask after thresholding"), "mask_empty"),
    (re.compile(r"^Lesion does not overlap any parcellation ROI"), "lesion_outside_parcellation"),
)
MESSAGES = {
    "mask_empty": "The lesion mask has no voxels set.",
    "lesion_outside_parcellation": (
        "The lesion does not overlap any region of the normative parcellation."
    ),
}


def parcellation_path(bundle: Path) -> Path:
    """The parcellation file of a ``normative-fc`` bundle (documented rule, in this order).

    1. ``metadata.parcellation`` of ``bundle.yaml``: a path relative to the bundle root
       (for example ``standard/firsT_2566.nii.gz``) that must name an existing ``.nii.gz`` file
       inside the bundle;
    2. otherwise the only ``*.nii.gz`` file directly under ``standard/``.

    Anything else (no file, several files and no metadata, a path leaving the bundle) is a
    ``bundle_layout_unsupported`` failure: the step never guesses between parcellations.
    """
    rel = None
    manifest = bundle / BUNDLE_MANIFEST
    if manifest.is_file():
        doc = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        rel = (doc.get("metadata") or {}).get("parcellation")
    if rel is not None:
        path = (bundle / str(rel)).resolve()
        inside = path.is_relative_to(bundle.resolve())
        if not inside or not str(rel).endswith(PARCELLATION_SUFFIX) or not path.is_file():
            raise InternalFailure(
                "bundle_layout_unsupported",
                "The parcellation named by the bundle metadata is not a .nii.gz file of the "
                "bundle.",
            )
        return path
    found = sorted(
        p for p in (bundle / PARCELLATION_DIR).glob("*" + PARCELLATION_SUFFIX) if p.is_file()
    )
    if len(found) != 1:
        raise InternalFailure(
            "bundle_layout_unsupported",
            "The bundle does not identify one parcellation: set metadata.parcellation in "
            f"bundle.yaml or keep exactly one {PARCELLATION_SUFFIX} file under "
            f"{PARCELLATION_DIR}/.",
            details={"parcellation_files_found": len(found)},
        )
    return found[0]


def _check_bundle(bundle: Path) -> Path:
    """Checks the layout of the normative bundle; returns the parcellation file."""
    for rel in (FC_FILE, SUBJECTS_FILE):
        if not (bundle / rel).is_file():
            raise InternalFailure(
                "bundle_layout_unsupported",
                "The normative bundle does not have the layout of a normative-fc bundle.",
                details={"missing": rel},
            )
    parcellation = parcellation_path(bundle)
    img = nib.load(str(parcellation))
    grid = contracts.registry()["grids"][GRID]
    if list(img.shape) != list(grid["shape"]) or not np.allclose(
        img.affine, np.asarray(grid["affine"], dtype=float), rtol=0, atol=1e-4
    ):
        raise InternalFailure(
            "bundle_layout_unsupported",
            "The parcellation of the normative bundle is not on the working grid.",
        )
    _check_subjects(bundle / FC_FILE, bundle / SUBJECTS_FILE)
    return parcellation


def _check_subjects(fc_file: Path, subjects_csv: Path) -> None:
    """Every participant of the FC array has exactly one row of the table (the engine requires
    it when it does not match by age and sex). The table needs no age or sex column; extra
    columns are ignored."""
    fc = np.load(fc_file, mmap_mode="r")
    if fc.ndim != 3 or fc.shape[0] != fc.shape[2]:
        raise InternalFailure(
            "bundle_layout_unsupported",
            "The connectivity array is not (parcels x participants x parcels).",
            details={"ndim": int(fc.ndim)},
        )
    with subjects_csv.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "subject_index" not in reader.fieldnames:
            raise InternalFailure(
                "bundle_layout_unsupported", "The normative table has no subject_index column."
            )
        try:
            indices = sorted(int(row["subject_index"]) for row in reader)
        except (TypeError, ValueError):
            raise InternalFailure(
                "bundle_layout_unsupported", "The normative table has a non-integer subject_index."
            ) from None
    if indices != list(range(int(fc.shape[1]))):
        raise InternalFailure(
            "bundle_layout_unsupported",
            "The normative table must have one row for every participant of the connectivity "
            "array (subject_index 0 to n-1, no gaps or duplicates).",
            details={"table_rows": len(indices), "array_participants": int(fc.shape[1])},
        )


def _scrub(text: str) -> str:
    """Engine text for ``details``: absolute paths removed, whitespace collapsed, truncated."""
    return " ".join(PATHS.sub("<path>", text).split())[:200]


def _engine_failures(path: Path) -> list[str]:
    if not path.is_file():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return [row[1] for row in list(csv.reader(fh))[1:] if len(row) > 1]


def _count_rows(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as fh:
        return max(sum(1 for _ in fh) - 1, 0)


def _undefined_parcels(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as fh:
        return sum(1 for row in csv.DictReader(fh) if row["z"].strip().lower() == "nan")


def _record_software(ctx: StepContext) -> None:
    digest = hashlib.sha256(ENGINE_SCRIPT.read_bytes()).hexdigest()
    ctx.add_software("flnm-fast engine", "sha256:" + digest[:12])
    for package in ("numpy", "nibabel", "pandas", "scipy"):
        try:
            ctx.add_software(package, metadata.version(package))
        except metadata.PackageNotFoundError:
            pass


def run(ctx: StepContext) -> None:
    bundle = ctx.bundle_path(BUNDLE)
    parcellation = _check_bundle(bundle)

    work = ctx.tmp_dir / "flnm-fast"
    lesion_dir, out_dir = work / "lesion", work / "zmaps"
    lesion_dir.mkdir(parents=True)
    (lesion_dir / f"{PARTICIPANT_ID}.nii.gz").write_bytes(
        ctx.input_path("lesion_mask").read_bytes()
    )
    participants = work / "participants.tsv"
    participants.write_text(f"id\n{PARTICIPANT_ID}\n", encoding="utf-8")

    cmd = [
        sys.executable,
        str(ENGINE_SCRIPT),
        "--participants", str(participants),
        "--participant-id", PARTICIPANT_ID,
        "--lesion-dir", str(lesion_dir),
        "--parcellation", str(parcellation),
        "--normative-subjects", str(bundle / SUBJECTS_FILE),
        "--fc", str(bundle / FC_FILE),
        "--out-dir", str(out_dir),
    ]  # fmt: skip
    ctx.log.info("running the flnm-fast engine on one participant")
    proc = subprocess.run(
        cmd, cwd=work, capture_output=True, text=True, encoding="utf-8", check=False
    )
    if proc.returncode != 0:
        raise InternalFailure(
            "engine_failed",
            "The flnm-fast engine stopped with an error.",
            details={
                "engine_exit_code": proc.returncode,
                "engine_stderr": _scrub(proc.stderr),
            },
        )

    for text in _engine_failures(out_dir / "failures.csv"):
        for pattern, code in FAILURES:
            if pattern.match(text):
                raise InputRejected(code, MESSAGES[code], port="lesion_mask")
        raise InternalFailure(
            "engine_failed",
            "The flnm-fast engine could not process the participant.",
            details={"engine_error": _scrub(text)},
        )

    zmap = out_dir / f"{PARTICIPANT_ID}_zmap.nii.gz"
    tables = {
        name: out_dir / f"{PARTICIPANT_ID}_{name}.csv" for name in ("overlap", "controls", "roi_z")
    }
    if not zmap.is_file() or not all(p.is_file() for p in tables.values()):
        raise InternalFailure(
            "engine_output_missing", "The flnm-fast engine did not write its outputs."
        )
    n_overlapped = _count_rows(tables["overlap"])
    ctx.log.info(
        "%d lesion-overlapped parcels used, %d normative participants",
        n_overlapped,
        _count_rows(tables["controls"]),
    )
    undefined = _undefined_parcels(tables["roi_z"])
    if undefined:
        ctx.warn(
            "undefined_parcels",
            f"{undefined} parcel(s) have an undefined z score (fewer than two finite "
            "connectivity values across the controls, or no variance); they are NaN in the map.",
            port="fdisconnection_map",
        )
    ctx.output_path("fdisconnection_map").write_bytes(zmap.read_bytes())
    _record_software(ctx)
