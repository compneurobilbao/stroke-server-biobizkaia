"""Cognitive disconnection profile: score and p-value per concept and per domain.

Output format: ``contracts/schemas/v1/cognet-profile.schema.json`` (artefact type
``cognet_profile``).

Scores and p-values come from the author's ``compute_cdr`` and ``compute_overlap``
(``upstream.py``), called on parcel vectors exactly as the author's workflow calls them. That
code averages with ``nanmean``, so parcels without a defined value (NaN) are left out of the
inside and outside means of the concept and of every null map. The wrapper only adds what the
contract needs around it:

* An undefined score (NaN: no defined parcel inside or outside the concept) is reported as
  ``null`` with a ``null`` p-value and ``flag: empty_mask``. The author's code would give such a
  score the p-value ``1 / n_nulls`` (no null map is larger than NaN), which would read as the
  strongest possible result; the wrapper does not report it.
* ``flag: zero_outside_mean`` when the defined mean outside the concept is zero (the author's
  code then returns the inside mean, not a ratio).
* ``excluded_parcels`` when the disconnection map has parcels without a defined value.

Multiple comparisons: per measure, the p-value of every concept is Bonferroni-corrected over all
concepts (as the author's workflow does). A domain's score is the mean of the finite scores of
its concepts; its p-value is the smallest p-value among its concepts times their number (at most
1), Bonferroni-corrected again over all domains.
"""

from __future__ import annotations

import math
import warnings

import numpy as np

from . import upstream
from .bundle import ConceptMaps, load_concept

PROFILE_VERSION = "1.1"
MEASURES = ("disconnection_positive", "disconnection_negative", "lesion_overlap")
EMPTY_MASK = "empty_mask"
ZERO_OUTSIDE_MEAN = "zero_outside_mean"
EXCLUDED_REASON = "undefined_in_disconnection_map"


def _num(value):
    value = float(value)
    return None if math.isnan(value) else value


def bonferroni(p_values, n_tests: int):
    """Bonferroni correction over ``n_tests`` tests; undefined p-values stay undefined."""
    return [None if p is None else min(1.0, p * n_tests) for p in p_values]


def split_disconnection(volume):
    """Positive part, and magnitude of the negative part, of a disconnection map. NaN voxels
    stay NaN in both parts (no defined value), as opposed to the author's workflow, which turns
    them into zeros before parcellating."""
    volume = np.asarray(volume, dtype=np.float64)
    undefined = np.isnan(volume)
    positive = np.where(volume > 0, volume, 0.0)
    negative = np.where(volume < 0, -volume, 0.0)
    positive[undefined] = np.nan
    negative[undefined] = np.nan
    return positive, negative


def cdr_flag(concept_mask: np.ndarray, values: np.ndarray) -> str | None:
    """Name the degenerate case of a CDR on the defined parcels, or None for a regular ratio."""
    defined = ~np.isnan(values)
    inside = defined & (concept_mask == 1)
    outside = defined & (concept_mask == -1)
    if not inside.any() or not outside.any():
        return EMPTY_MASK
    if np.mean(values[outside]) == 0:
        return ZERO_OUTSIDE_MEAN
    return None


def _call(function, *args):
    # The author's code warns ("Mean of empty slice", "invalid value") on the edge cases that
    # are reported through `flag` and null values; keep the step log free of them.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with np.errstate(invalid="ignore", divide="ignore"):
            return function(*args)


def _concept_row(concept, domain, mask, nulls, parcel_values, cdr) -> dict:
    row: dict = {"concept": concept, "domain": domain}
    for measure in MEASURES:
        values = parcel_values[measure]
        if measure == "lesion_overlap":
            score, p = _call(cdr.compute_overlap, mask, values, nulls)
            flag = None
        else:
            score, p = _call(cdr.compute_cdr, mask, values, nulls)
            flag = cdr_flag(mask, values)
        score = _num(score)
        p = None if score is None else _num(p)
        result = {"score": score, "p_value": p}
        if flag is not None:
            result["flag"] = flag
        row[measure] = result
    return row


def _domain_rows(concept_rows) -> list:
    order: list = []
    for row in concept_rows:
        if row["domain"] not in order:
            order.append(row["domain"])
    rows = []
    for domain in order:
        members = [r for r in concept_rows if r["domain"] == domain]
        out: dict = {"domain": domain, "n_concepts": len(members)}
        for measure in MEASURES:
            scores = [m[measure]["score"] for m in members if m[measure]["score"] is not None]
            ps = [m[measure]["p_value"] for m in members if m[measure]["p_value"] is not None]
            out[measure] = {
                "score": float(np.mean(scores)) if scores else None,
                "p_value": min(1.0, min(ps) * len(members)) if ps else None,
            }
        rows.append(out)
    return rows


def _finish(rows, alpha: float) -> None:
    """Add p_corrected (Bonferroni over the rows) and significant to every measure."""
    for measure in MEASURES:
        corrected = bonferroni([r[measure]["p_value"] for r in rows], len(rows))
        for r, pc in zip(rows, corrected):
            r[measure]["p_corrected"] = pc
            r[measure]["significant"] = pc is not None and pc < alpha


def excluded_parcels(parcel_values: dict, n_parcels: int) -> dict | None:
    """``excluded_parcels`` of the profile, or None when every parcel has a defined value."""
    undefined = np.isnan(parcel_values["disconnection_positive"]) | np.isnan(
        parcel_values["disconnection_negative"]
    )
    count = int(np.count_nonzero(undefined))
    if count == 0:
        return None
    return {"count": count, "fraction": count / n_parcels, "reason": EXCLUDED_REASON}


def build_profile(maps: ConceptMaps, parcel_values: dict, alpha: float, bundle_record: dict) -> dict:
    """Compute the profile.

    ``parcel_values`` maps each name in ``MEASURES`` to a parcel vector: the parcel means of the
    defined voxels of the positive and of the negative part of the disconnection map (NaN for a
    parcel without any), and the 0/1 indicator of the parcels touched by the lesion.
    """
    cdr = upstream.load()
    concept_rows = []
    n_nulls = None
    for concept, domain in maps.concepts:
        mask, nulls = load_concept(maps, concept)
        if n_nulls is None:
            n_nulls = int(nulls.shape[0])
        elif nulls.shape[0] != n_nulls:
            raise ValueError("all concepts must have the same number of null maps")
        concept_rows.append(_concept_row(concept, domain, mask, nulls, parcel_values, cdr))
    _finish(concept_rows, alpha)
    domain_rows = _domain_rows(concept_rows)
    _finish(domain_rows, alpha)
    profile = {
        "kind": "CognetProfile",
        "profile_version": PROFILE_VERSION,
        "method": {
            "name": "concept-to-disconnection-representation",
            "alpha": alpha,
            "correction": "bonferroni",
            "n_nulls": n_nulls,
            "n_parcels": maps.n_parcels,
            "measures": list(MEASURES),
        },
        "bundle": bundle_record,
        "concepts": concept_rows,
        "domains": domain_rows,
    }
    excluded = excluded_parcels(parcel_values, maps.n_parcels)
    if excluded is not None:
        profile["excluded_parcels"] = excluded
    return profile
