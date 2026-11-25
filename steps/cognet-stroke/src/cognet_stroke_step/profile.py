"""Cognitive disconnection profile: score and p-value per concept and per domain.

Output format: ``contracts/schemas/v1/cognet-profile.schema.json`` (artefact type
``cognet_profile``).

Scores and p-values come from the author's ``compute_cdr`` and ``compute_overlap``
(``upstream.py``), called on parcel vectors exactly as the author's workflow calls them. That
code averages with ``nanmean``, so parcels without a defined value (NaN) are left out of the
inside and outside means of the concept and of every null map, and its p-value is the fraction
of null maps with a strictly larger value, at least ``1 / n_nulls``. The wrapper never changes
that formula; it only adds what the contract needs around it:

* An undefined score (NaN: no defined parcel inside or outside the concept) is reported as
  ``null`` with a ``null`` p-value and ``flag: empty_mask``. The author's code would give such a
  score the p-value ``1 / n_nulls`` (no null map is larger than NaN), which would read as the
  strongest possible result.
* ``flag: zero_outside_mean`` when the defined mean outside the concept is zero (the author's
  code then returns the inside mean, not a ratio).
* **Degenerate null distribution.** When the defined null values of a concept have no spread
  (all equal, which includes "the observed value equals every null", for example a map that is
  zero everywhere in one of its parts), or no null value is defined, no null can exceed the
  observed value and the author's rule gives ``1 / n_nulls`` whatever the data. The wrapper
  then reports the p-value as ``null`` (so ``significant`` is false), keeps the score, and lists
  the case in the warning ``degenerate_null_distribution``. (A dedicated ``flag`` value needs a
  change of the profile schema, a contract change; proposed as a follow-up.)
* Null maps without a defined value (all of their inside or all of their outside parcels
  undefined) stay in the denominator of the author's p-value, as the author's code does; the
  wrapper counts them and reports them in the warning ``undefined_null_maps``.
* Ties: the author's rule counts only nulls strictly larger than the observed value. When none is
  larger but some are equal, the p-value is ``1 / n_nulls`` although part of the null
  distribution reaches the observed value (typically a lesion overlap of 1.0). The value is kept
  and the case is listed in the warning ``tied_null_values``.
* ``excluded_parcels`` when the disconnection map has parcels without a defined value.

The null values used for these checks are computed by the author's own functions, called with
each null mask as the concept mask (the computation the author's code performs internally); the
wrapper checks that they reproduce the author's p-value.

Multiple comparisons: per measure, the p-value of every concept is Bonferroni-corrected over all
concepts (as the author's workflow does). A domain's score is the mean of the finite scores of
its concepts; its p-value is the smallest p-value among its concepts times their number (at most
1), Bonferroni-corrected again over all domains.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, field

import numpy as np

from . import upstream
from .bundle import ConceptMaps, load_concept

PROFILE_VERSION = "1.1"
MEASURES = ("disconnection_positive", "disconnection_negative", "lesion_overlap")
EMPTY_MASK = "empty_mask"
ZERO_OUTSIDE_MEAN = "zero_outside_mean"
EXCLUDED_REASON = "undefined_in_disconnection_map"


@dataclass
class Diagnostics:
    """Findings about the null distributions, reported as provenance warnings by the step."""

    n_nulls: int = 0
    degenerate: list = field(default_factory=list)  # [(concept, measure)]
    undefined_nulls: list = field(default_factory=list)  # [(concept, measure, count)]
    ties: list = field(default_factory=list)  # [(concept, measure, count)]

    def warnings(self) -> list[tuple[str, str]]:
        """``[(code, message)]`` for ``ctx.warn`` (messages at most 500 characters)."""
        out = []
        if self.degenerate:
            out.append(
                (
                    "degenerate_null_distribution",
                    _listing(
                        f"{len(self.degenerate)} concept scores have a null distribution "
                        "without spread (or no defined null value), so their p-value is "
                        "reported as null and they are not significant",
                        [f"{c}:{m}" for c, m in self.degenerate],
                    ),
                )
            )
        if self.undefined_nulls:
            worst = max(n for _c, _m, n in self.undefined_nulls)
            out.append(
                (
                    "undefined_null_maps",
                    _listing(
                        f"{len(self.undefined_nulls)} concept scores have null maps without a "
                        f"defined value (at most {worst} of {self.n_nulls}); they count in the "
                        "p-value denominator as in the author's code",
                        [f"{c}:{m}={n}" for c, m, n in self.undefined_nulls],
                    ),
                )
            )
        if self.ties:
            out.append(
                (
                    "tied_null_values",
                    _listing(
                        f"{len(self.ties)} concept scores have no larger null value but null "
                        f"values equal to the observed one, so their p-value is 1/{self.n_nulls}"
                        " by the strict rule of the author's code",
                        [f"{c}:{m}={n}" for c, m, n in self.ties],
                    ),
                )
            )
        return out


def _listing(head: str, items: list[str], limit: int = 500) -> str:
    text = head + ": " + ", ".join(items)
    if len(text) <= limit:
        return text
    shown = []
    for item in items:
        candidate = head + ": " + ", ".join(shown + [item]) + f" and {len(items)} more"
        if len(candidate) > limit:
            break
        shown.append(item)
    return head + ": " + ", ".join(shown) + f" and {len(items) - len(shown)} more"


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
    # are reported through `flag`, null values and warnings; keep the step log free of them.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        with np.errstate(invalid="ignore", divide="ignore"):
            return function(*args)


def null_values(function, nulls: np.ndarray, values: np.ndarray) -> np.ndarray:
    """The value of every null mask, computed by the author's ``function`` with the null mask as
    the concept mask (what the author's code computes for each null internally)."""
    return np.array([float(_call(function, null, values)) for null in nulls], dtype=np.float64)


def _concept_row(concept, domain, mask, nulls, parcel_values, cdr, diagnostics=None) -> dict:
    row: dict = {"concept": concept, "domain": domain}
    for measure in MEASURES:
        values = parcel_values[measure]
        if measure == "lesion_overlap":
            function = cdr.compute_overlap
            flag = None
        else:
            function = cdr.compute_cdr
            flag = cdr_flag(mask, values)
        score, p = _call(function, mask, values, nulls)
        score = _num(score)
        p = None if score is None else _num(p)
        if score is not None:
            nv = null_values(function, nulls, values)
            defined = nv[~np.isnan(nv)]
            hits = int(np.count_nonzero(defined > score))
            if p is not None and not math.isclose(p, max(hits, 1) / nv.size, rel_tol=0, abs_tol=1e-12):
                raise RuntimeError(
                    f"{concept}/{measure}: the null values do not reproduce the author's p-value"
                )
            if diagnostics is not None:
                n_undefined = int(nv.size - defined.size)
                if n_undefined:
                    diagnostics.undefined_nulls.append((concept, measure, n_undefined))
            if defined.size == 0 or np.all(defined == defined[0]):
                p = None  # degenerate: the strict rule gives 1 / n_nulls whatever the data
                if diagnostics is not None:
                    diagnostics.degenerate.append((concept, measure))
            elif hits == 0 and diagnostics is not None:
                ties = int(np.count_nonzero(defined == score))
                if ties:
                    diagnostics.ties.append((concept, measure, ties))
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


def build_profile(
    maps: ConceptMaps, parcel_values: dict, alpha: float, bundle_record: dict
) -> tuple[dict, Diagnostics]:
    """Compute the profile and the diagnostics of its null distributions.

    ``parcel_values`` maps each name in ``MEASURES`` to a parcel vector: the parcel means of the
    defined voxels of the positive and of the negative part of the disconnection map (NaN for a
    parcel without any), and the 0/1 indicator of the parcels touched by the lesion.
    """
    cdr = upstream.load()
    diagnostics = Diagnostics()
    concept_rows = []
    n_nulls = None
    for concept, domain in maps.concepts:
        mask, nulls = load_concept(maps, concept)
        if n_nulls is None:
            n_nulls = int(nulls.shape[0])
        elif nulls.shape[0] != n_nulls:
            raise ValueError("all concepts must have the same number of null maps")
        concept_rows.append(
            _concept_row(concept, domain, mask, nulls, parcel_values, cdr, diagnostics)
        )
    diagnostics.n_nulls = n_nulls or 0
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
    return profile, diagnostics
