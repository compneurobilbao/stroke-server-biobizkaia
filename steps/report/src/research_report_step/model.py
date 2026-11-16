"""Assemble the content of the report (the dictionary written as report.json).

Everything here is a pure function of the staged inputs: no clock, no randomness, no
software versions, so equal inputs give an identical report.json. Missing optional
inputs produce a section with status ``absent`` instead of an error.
"""

from __future__ import annotations

import csv
from pathlib import Path

import nibabel as nib
import numpy as np

REPORT_VERSION = "1.0"
RESEARCH_USE_ONLY = "For research use only, not for clinical decisions."
MEASURES = ("disconnection_positive", "disconnection_negative", "lesion_overlap")

LESION_QC_REQUIRED = (
    "mode",
    "n_voxels",
    "volume_ml",
    "hemisphere",
    "components",
    "bounding_box",
    "centroid_mm",
    "warnings",
    "grid",
)


class InvalidInput(ValueError):
    """An input has the right type but unusable content (maps to input_format_invalid)."""


def _num(x, nd: int | None = None) -> float:
    x = float(x)
    return round(x, nd) if nd is not None else x


# --- lesion ----------------------------------------------------------------------


def mask_stats(path: Path) -> dict:
    img = nib.load(str(path))
    data = np.asanyarray(img.dataobj)
    idx = np.argwhere(data > 0)
    if len(idx) == 0:
        raise InvalidInput("the lesion mask has no voxels set")
    voxel_ml = abs(float(np.linalg.det(img.affine[:3, :3]))) / 1000.0
    centroid = nib.affines.apply_affine(img.affine, idx.mean(axis=0))
    return {
        "n_voxels": int(len(idx)),
        "volume_ml": _num(len(idx) * voxel_ml, 4),
        "centroid_mm": [_num(v, 2) for v in centroid],
    }


def lesion_section(lesion_qc: dict, mask: dict) -> dict:
    missing = [k for k in LESION_QC_REQUIRED if k not in lesion_qc]
    if missing:
        raise InvalidInput("lesion_qc lacks: " + ", ".join(missing))
    hemi = lesion_qc["hemisphere"]
    comps = lesion_qc["components"]
    return {
        "status": "available",
        "qc_mode": lesion_qc["mode"],
        "n_voxels": int(lesion_qc["n_voxels"]),
        "volume_ml": lesion_qc["volume_ml"],
        "hemisphere": hemi["label"],
        "hemisphere_voxels": {
            "left": hemi.get("left_voxels"),
            "right": hemi.get("right_voxels"),
            "midline": hemi.get("midline_voxels"),
        },
        "components": {
            "count": int(comps["count"]),
            "largest_fraction": comps.get("largest_fraction"),
            "volumes_ml": list(comps.get("volumes_ml", [])),
        },
        "centroid_mm": list(lesion_qc["centroid_mm"]),
        "bounding_box_mm": lesion_qc["bounding_box"].get("world_mm"),
        "grid": {
            "name": lesion_qc["grid"].get("name"),
            "shape": list(lesion_qc["grid"].get("shape", [])),
            "voxel_size_mm": list(lesion_qc["grid"].get("voxel_size_mm", [])),
        },
        "qc_warnings": [
            {"code": w["code"], "message": w["message"]} for w in lesion_qc["warnings"]
        ],
        "mask_on_working_grid": mask,
        "mask_agrees_with_qc": abs(mask["volume_ml"] - float(lesion_qc["volume_ml"])) < 0.001
        and mask["n_voxels"] == int(lesion_qc["n_voxels"]),
    }


# --- disconnection -----------------------------------------------------------------


def read_tract_table(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        rows = []
        for n, row in enumerate(reader, start=2):
            try:
                value = float(row["disconnection"])
            except (KeyError, TypeError, ValueError):
                raise InvalidInput(f"tract table row {n}: disconnection is not a number") from None
            if not 0.0 <= value <= 1.0:  # also false for NaN
                raise InvalidInput(f"tract table row {n}: disconnection outside [0, 1]")
            name = (row.get("tract") or "").strip()
            if not name:
                raise InvalidInput(f"tract table row {n}: empty tract name")
            rows.append({"tract": name, "disconnection": value})
    if not rows:
        raise InvalidInput("the tract table has no rows")
    names = [r["tract"] for r in rows]
    if len(set(names)) != len(names):
        raise InvalidInput("the tract table has duplicate tract names")
    rows.sort(key=lambda r: (-r["disconnection"], r["tract"]))
    return rows


def tract_section(rows: list[dict] | None) -> dict:
    if rows is None:
        return {"status": "absent"}
    values = [r["disconnection"] for r in rows]
    return {
        "status": "available",
        "n_tracts": len(rows),
        "max_tract": rows[0]["tract"],
        "max_disconnection": rows[0]["disconnection"],
        "mean_disconnection": sum(values) / len(values),
        "n_tracts_above_0_5": sum(1 for v in values if v > 0.5),
        "tracts": rows,
    }


def fdisconnection_stats(path: Path) -> dict:
    img = nib.load(str(path))
    data = np.asanyarray(img.dataobj).astype(np.float64)
    if not np.all(np.isfinite(data)):
        raise InvalidInput("the functional disconnection map has non-finite values")
    pos = data > 0
    n = int(pos.sum())
    return {
        "status": "available",
        "max": _num(data.max(), 4),
        "n_positive_voxels": n,
        "mean_of_positive": _num(data[pos].mean(), 4) if n else 0.0,
    }


# --- cognitive profile ---------------------------------------------------------------


def cognet_section(profile: dict | None) -> dict:
    if profile is None:
        return {"status": "absent"}
    sig_concepts = []
    for c in profile["concepts"]:
        flags = [m for m in MEASURES if c[m]["significant"]]
        if flags:
            sig_concepts.append(
                {
                    "concept": c["concept"],
                    "domain": c["domain"],
                    "significant_measures": flags,
                    **{m: c[m] for m in MEASURES},
                }
            )
    sig_concepts.sort(key=lambda c: (c["domain"], c["concept"]))
    return {
        "status": "available",
        "profile_version": profile["profile_version"],
        "method": profile["method"],
        "bundle": profile["bundle"],
        "n_concepts": len(profile["concepts"]),
        "domains": sorted(profile["domains"], key=lambda d: d["domain"]),
        "significant_concepts": sig_concepts,
    }


# --- provenance --------------------------------------------------------------------


def _image_digest(image: str | None) -> str | None:
    if image and "@sha256:" in image:
        return "sha256:" + image.split("@sha256:", 1)[1]
    return None


def provenance_section(run: dict) -> dict:
    """Run provenance without the accounting block (user and billing identifiers)."""
    slots = []
    for s in run.get("slots", []):
        rec = {
            "slot": s["slot"],
            "status": s["status"],
            "reason": s.get("reason"),
            "implementation": None,
        }
        impl = s.get("implementation")
        if impl:
            rec["implementation"] = {
                "id": impl["id"],
                "step_name": impl["step"]["name"],
                "step_version": impl["step"]["version"],
                "image": impl["image"],
                "image_digest": _image_digest(impl["image"]),
                "stub": bool(impl["stub"]),
                "chosen_by": impl["chosen_by"],
            }
        rec["bundles"] = [
            {k: b[k] for k in ("name", "id", "version", "content_digest")}
            for b in s.get("bundles", [])
        ]
        slots.append(rec)
    steps = []
    for st in run.get("steps", []):
        prov = st.get("provenance") or {}
        execution = st.get("execution") or {}
        steps.append(
            {
                "slot": st["slot"],
                "step_name": (prov.get("step") or {}).get("name"),
                "step_version": (prov.get("step") or {}).get("version"),
                "image": execution.get("image"),
                "outcome": execution.get("outcome"),
                "exit_code": execution.get("exit_code"),
                "software": list(prov.get("software", [])),
                "inputs": [
                    {"port": a["port"], "sha256": a["sha256"]} for a in prov.get("inputs", [])
                ],
                "outputs": [
                    {"port": a["port"], "sha256": a["sha256"]} for a in prov.get("outputs", [])
                ],
            }
        )
    return {
        "run": {
            "run_id": run["run_id"],
            "case_id": run["case_id"],
            "pipeline": run["pipeline"],
            "platform": run["platform"],
            "status": run["status"],
            "created_at": run["created_at"],
            "seed": run.get("seed"),
            "input_set": run.get("input_set"),
        },
        "slots": slots,
        "steps": steps,
    }


# --- whole report --------------------------------------------------------------------


def build_report(
    *,
    language: str,
    step: dict,
    run_provenance: dict,
    inputs: list[dict],
    lesion: dict,
    functional: dict,
    tracts: dict,
    cognet: dict,
) -> dict:
    sections = {
        "lesion": lesion["status"],
        "functional_disconnection": functional["status"],
        "tract_disconnection": tracts["status"],
        "cognitive_profile": cognet["status"],
    }
    notes = []
    if not lesion["mask_agrees_with_qc"]:
        notes.append("lesion_mask_differs_from_qc")
    if cognet.get("bundle", {}).get("synthetic"):
        notes.append("synthetic_reference_data")
    return {
        "kind": "ResearchReport",
        "report_version": REPORT_VERSION,
        "language": language,
        "research_use_only": True,
        "research_use_statement": RESEARCH_USE_ONLY,
        "generator": {"step": step["name"], "version": step["version"]},
        "sections": sections,
        "notes": notes,
        "lesion": lesion,
        "functional_disconnection": functional,
        "tract_disconnection": tracts,
        "cognitive_profile": cognet,
        "provenance": run_provenance,
        "inputs": inputs,
    }
