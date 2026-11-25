"""Contract wrapper around the COGNET-STROKE code (``src/cdr.py`` of the author's repository).

Flow: check inputs -> read the ``concept-maps`` bundle -> reduce the lesion and the positive and
negative parts of the disconnection map to the bundle's parcels -> profile (the author's
``compute_cdr`` and ``compute_overlap`` per concept, aggregated per domain) -> write the JSON
profile and the figure.
"""

from __future__ import annotations

import importlib.metadata as md
import json

import nibabel as nib
import numpy as np
import yaml
from jsonschema import Draft202012Validator

from stroke_step_sdk import (
    InputRejected,
    InternalFailure,
    QcFailed,
    StepContext,
    contracts,
)

from . import upstream
from .bundle import BundleLayoutError, load_concept_maps
from .figure import profile_figure
from .parcels import defined_fraction, parcellate_mean, touched
from .profile import build_profile, split_disconnection

BUNDLE = "concept_maps"
PROFILE_SCHEMA = "schemas/v1/cognet-profile.schema.json"


def working_grid():
    registry = yaml.safe_load(
        (contracts.contracts_dir() / "artefact-types.yaml").read_text(encoding="utf-8")
    )
    grid = registry["grids"]["MNI152NLin6Asym-res2"]
    return tuple(grid["shape"]), np.array(grid["affine"], dtype=float)


def read_inputs(ctx: StepContext):
    lesion_img = nib.load(str(ctx.input_path("lesion_mask")))
    disc_img = nib.load(str(ctx.input_path("fdisconnection_map")))
    lesion = np.asanyarray(lesion_img.dataobj) > 0
    if not lesion.any():
        raise InputRejected(
            "mask_empty", "The lesion mask has no voxels set.", port="lesion_mask"
        )
    disc = np.asanyarray(disc_img.dataobj).astype(np.float64)
    if np.isinf(disc).any():
        raise InputRejected(
            "disconnection_not_finite",
            "The disconnection map contains infinite values.",
            port="fdisconnection_map",
        )
    defined = ~np.isnan(disc)
    if not defined.any():
        raise InputRejected(
            "disconnection_map_undefined",
            "The disconnection map has no defined value.",
            port="fdisconnection_map",
        )
    if not disc[defined].any():
        raise QcFailed(
            "disconnection_map_empty",
            "The disconnection map is zero wherever it is defined, so no profile can be computed.",
            port="fdisconnection_map",
        )
    return lesion_img, lesion, disc


def validate_profile(profile: dict, ctx: StepContext) -> None:
    """Check the document against the contract schema shipped with the image."""
    path = contracts.contracts_dir() / PROFILE_SCHEMA
    if not path.is_file():
        ctx.log.warning("profile schema not found in the contracts directory; not checked")
        return
    schema = json.loads(path.read_text(encoding="utf-8"))
    errors = list(Draft202012Validator(schema).iter_errors(profile))
    if errors:
        raise InternalFailure(
            "profile_schema_violation",
            "The computed profile does not satisfy its schema.",
            details={"first_error": errors[0].message[:200]},
        )


def _bundle_error(exc: Exception) -> InternalFailure:
    return InternalFailure(
        "bundle_layout_invalid",
        "The concept-map bundle does not follow the layout the step expects.",
        details={"reason": str(exc)[:200]},
    )


def run(ctx: StepContext) -> None:
    shape, affine = working_grid()
    lesion_img, lesion, disc = read_inputs(ctx)
    try:
        upstream.load()
    except upstream.UpstreamUnavailable as exc:
        raise InternalFailure(
            "upstream_unavailable",
            "The COGNET-STROKE code is missing from the step image.",
            details={"reason": str(exc)[:200]},
        ) from exc

    bundle_dir = ctx.bundle_path(BUNDLE)
    bundle_doc = yaml.safe_load((bundle_dir / "bundle.yaml").read_text(encoding="utf-8"))
    try:
        maps = load_concept_maps(bundle_dir, shape, affine)
    except BundleLayoutError as exc:
        raise _bundle_error(exc) from exc
    ctx.log.info(
        "bundle %s %s: %d parcels, %d concepts",
        bundle_doc["id"], bundle_doc["version"], maps.n_parcels, len(maps.concepts),
    )

    positive, negative = split_disconnection(disc)
    values = {
        "disconnection_positive": parcellate_mean(positive, maps.labels, maps.ids),
        "disconnection_negative": parcellate_mean(negative, maps.labels, maps.ids),
        # a parcel is touched by the lesion if any of its voxels is lesioned
        "lesion_overlap": touched(lesion, maps.labels, maps.ids),
    }
    undefined = int(np.isnan(values["disconnection_positive"]).sum())
    if undefined == maps.n_parcels:
        raise InputRejected(
            "disconnection_map_undefined",
            "The disconnection map has no defined value in any parcel of the concept-map parcellation.",
            port="fdisconnection_map",
        )
    if undefined:
        ctx.warn(
            "parcels_excluded",
            f"{undefined} of {maps.n_parcels} parcels have no defined value in the disconnection "
            "map and are left out of the disconnection scores.",
            port="fdisconnection_map",
        )
    share = defined_fraction(disc, maps.labels, maps.ids)
    partly = (share > 0) & (share < 1)
    if partly.any():
        # Reported, not filtered: a minimum-coverage rule would change which parcels count, a
        # choice for the method's author (no such rule exists in the author's workflow).
        ctx.warn(
            "parcels_partly_defined",
            f"{int(partly.sum())} of {maps.n_parcels} parcels have a defined value on only part "
            f"of their voxels (lowest share {100 * float(share[partly].min()):.1f} %); their "
            "mean uses the defined voxels.",
            port="fdisconnection_map",
        )
    if not values["lesion_overlap"].any():
        raise QcFailed(
            "lesion_outside_parcellation",
            "The lesion does not overlap any parcel of the concept-map parcellation.",
            port="lesion_mask",
        )
    ctx.log.info("lesion touches %d of %d parcels", int(values["lesion_overlap"].sum()), maps.n_parcels)

    record = {
        "id": bundle_doc["id"],
        "version": bundle_doc["version"],
        "content_digest": bundle_doc["content_digest"],
    }
    if bundle_doc.get("synthetic"):
        record["synthetic"] = True
    try:
        profile, diagnostics = build_profile(maps, values, float(ctx.params["alpha"]), record)
    except (BundleLayoutError, ValueError) as exc:
        raise _bundle_error(exc) from exc
    for code, message in diagnostics.warnings():
        ctx.warn(code, message, port="profile")
    validate_profile(profile, ctx)

    ctx.output_path("profile").write_text(
        json.dumps(profile, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8"
    )
    profile_figure(ctx.output_path("profile_figure"), disc, lesion, profile)

    ctx.add_software(upstream.NAME, f"git-{upstream.commit()}")
    for name in ("numpy", "nibabel", "matplotlib"):
        ctx.add_software(name, md.version(name))
