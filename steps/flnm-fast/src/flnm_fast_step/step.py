"""Functional disconnection: contract wrapper around the packaged flnm-fast engine.

The engine (``/opt/lesion_zmaps/create_lesion_zmaps.py`` of the base image
``compneurobilbaolab/flnm-fast``) is run unchanged, as a subprocess, on one participant:
this module only maps the contract (``/in``, ``/ref``, parameters, ``/out``) onto the
engine's command line and its output files, and the engine's per-participant failures onto
declared error codes. It computes nothing itself.

Mapping:

* input ``lesion_mask`` becomes ``<tmp>/lesion/case.nii.gz``;
* parameters ``sex`` and ``age_band`` become the single row of ``<tmp>/participants.tsv``
  (age = midpoint of the band; sex written with the label the normative table uses) and
  ``--match-age-sex`` selects the ``control_count`` same-sex, nearest-age controls (engine 2.x
  uses every normative participant without it and then ignores ``control_count``);
* bundle ``normative`` supplies ``--fc``, ``--normative-subjects`` and ``--parcellation``;
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

from stroke_step_sdk import InputRejected, InternalFailure, StepContext, contracts

ENGINE_SCRIPT = Path("/opt/lesion_zmaps/create_lesion_zmaps.py")
BUNDLE = "normative"
FC_FILE = "fc_row_subject_col.npy"
SUBJECTS_FILE = "normative_subjects.csv"
PARCELLATION_FILE = "standard/schaefer1000_tian54_2mm.nii.gz"
GRID = "MNI152NLin6Asym-res2"
PARTICIPANT_ID = "case"
SEX_PREFIX = {"female": "F", "male": "M"}
PATHS = re.compile(r"(?:/[\w.@+=,~-]+)+/?")
BAND = re.compile(r"^([0-9]{1,3})-([0-9]{1,3})$")

# Engine failure texts (str of the exception, written to failures.csv) -> declared code.
FAILURES = (
    (re.compile(r"^Only \d+ normative subjects with sex="), "insufficient_controls"),
    (re.compile(r"^Empty lesion mask after thresholding"), "mask_empty"),
    (re.compile(r"^Lesion does not overlap any parcellation ROI"), "lesion_outside_parcellation"),
)
MESSAGES = {
    "insufficient_controls": (
        "The normative dataset has fewer participants of the case's sex than the number of "
        "controls requested."
    ),
    "mask_empty": "The lesion mask has no voxels set.",
    "lesion_outside_parcellation": (
        "The lesion does not overlap any region of the normative parcellation."
    ),
}


def age_from_band(band: str) -> float:
    """Midpoint of an age band such as ``65-69`` (the age the controls are matched to)."""
    match = BAND.match(band)
    if not match or int(match.group(1)) > int(match.group(2)):
        raise InputRejected(
            "invalid_parameters", "The age band is not a valid range of whole years."
        )
    return (int(match.group(1)) + int(match.group(2))) / 2.0


def normative_sex_label(subjects_csv: Path, sex: str) -> str:
    """The sex label of the normative table (as the engine reads it) for ``female``/``male``."""
    with subjects_csv.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames or "sex" not in reader.fieldnames:
            raise InternalFailure(
                "bundle_layout_unsupported", "The normative table has no sex column."
            )
        labels = {row["sex"].upper() for row in reader}
    wanted = SEX_PREFIX[sex]
    matching = sorted(label for label in labels if label[:1] == wanted)
    if len(matching) != 1:
        raise InternalFailure(
            "bundle_layout_unsupported",
            "The sex labels of the normative table do not identify female and male participants.",
            details={"labels_found": len(labels)},
        )
    return matching[0]


def _check_bundle(bundle: Path) -> None:
    for rel in (FC_FILE, SUBJECTS_FILE, PARCELLATION_FILE):
        if not (bundle / rel).is_file():
            raise InternalFailure(
                "bundle_layout_unsupported",
                "The normative bundle does not have the layout of a normative-fc bundle.",
                details={"missing": rel},
            )
    img = nib.load(str(bundle / PARCELLATION_FILE))
    grid = contracts.registry()["grids"][GRID]
    if list(img.shape) != list(grid["shape"]) or not np.allclose(
        img.affine, np.asarray(grid["affine"], dtype=float), rtol=0, atol=1e-4
    ):
        raise InternalFailure(
            "bundle_layout_unsupported",
            "The parcellation of the normative bundle is not on the working grid.",
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
    sex = ctx.params["sex"]
    band = ctx.params["age_band"]
    if sex is None:
        raise InputRejected(
            "participant_sex_missing",
            "The sex of the case is needed to select the normative controls; record it on the "
            "case and run again.",
        )
    if band is None:
        raise InputRejected(
            "participant_age_missing",
            "The age band of the case is needed to select the normative controls; record it on "
            "the case and run again.",
        )
    age = age_from_band(band)
    bundle = ctx.bundle_path(BUNDLE)
    _check_bundle(bundle)
    label = normative_sex_label(bundle / SUBJECTS_FILE, sex)

    work = ctx.tmp_dir / "flnm-fast"
    lesion_dir, out_dir = work / "lesion", work / "zmaps"
    lesion_dir.mkdir(parents=True)
    (lesion_dir / f"{PARTICIPANT_ID}.nii.gz").write_bytes(
        ctx.input_path("lesion_mask").read_bytes()
    )
    participants = work / "participants.tsv"
    participants.write_text(f"id\tage\tsex\n{PARTICIPANT_ID}\t{age}\t{label}\n", encoding="utf-8")

    cmd = [
        sys.executable,
        str(ENGINE_SCRIPT),
        "--participants", str(participants),
        "--participant-id", PARTICIPANT_ID,
        "--lesion-dir", str(lesion_dir),
        "--parcellation", str(bundle / PARCELLATION_FILE),
        "--normative-subjects", str(bundle / SUBJECTS_FILE),
        "--fc", str(bundle / FC_FILE),
        "--out-dir", str(out_dir),
        "--match-age-sex",
        "--control-count", str(int(ctx.params["control_count"])),
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
                raise InputRejected(
                    code,
                    MESSAGES[code],
                    port=None if code == "insufficient_controls" else "lesion_mask",
                )
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
        "%d lesion-overlapped parcels used, %d controls, control_count=%d",
        n_overlapped,
        _count_rows(tables["controls"]),
        ctx.params["control_count"],
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
