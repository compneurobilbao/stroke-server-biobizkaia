"""Voxel to parcel reduction on a parcellation image of the same grid."""

from __future__ import annotations

import numpy as np


def parcel_ids(labels: np.ndarray) -> np.ndarray:
    """Sorted non-zero integer labels of a parcellation array (0 is background)."""
    arr = np.asarray(labels)
    rounded = np.rint(arr)
    if not np.array_equal(arr, rounded):
        raise ValueError("the parcellation contains non-integer labels")
    ids = np.unique(rounded.astype(np.int64))
    return ids[ids > 0]


def parcellate_mean(data: np.ndarray, labels: np.ndarray, ids: np.ndarray) -> np.ndarray:
    """Mean of the defined (non-NaN) values of ``data`` over the voxels of every parcel in
    ``ids`` (same order). A parcel without any defined voxel is NaN: it has no value.

    On data without NaN this is the plain mean per parcel, computed exactly as step 1.0.0 did
    (sums and counts by ``np.bincount`` over the same voxels in the same order), so profiles of
    maps without NaN are unchanged. Infinite values are the caller's to reject.
    """
    data = np.asarray(data, dtype=np.float64)
    lab = np.rint(np.asarray(labels)).astype(np.int64)
    if data.shape != lab.shape:
        raise ValueError("data and parcellation must have the same shape")
    flat_lab = lab.ravel()
    flat = data.ravel()
    inside = np.isin(flat_lab, ids) & ~np.isnan(flat)
    index = np.searchsorted(ids, flat_lab[inside])
    sums = np.bincount(index, weights=flat[inside], minlength=ids.size)
    counts = np.bincount(index, minlength=ids.size)
    return np.divide(sums, counts, out=np.full(ids.size, np.nan), where=counts > 0)


def touched(mask: np.ndarray, labels: np.ndarray, ids: np.ndarray) -> np.ndarray:
    """1.0 for every parcel with at least one voxel set in ``mask``, else 0.0."""
    lab = np.rint(np.asarray(labels)).astype(np.int64).ravel()
    hit = np.unique(lab[np.asarray(mask, dtype=bool).ravel()])
    return np.isin(ids, hit).astype(float)
