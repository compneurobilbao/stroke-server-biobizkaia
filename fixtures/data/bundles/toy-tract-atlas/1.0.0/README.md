# Toy tract atlas (synthetic)

Four synthetic tracts, defined as bundles of curved streamlines in MNI152NLin6Asym
millimetre coordinates. Placeholder layout until the tract-wise disconnection step
fixes the real one.

- `tracts.tsv`: `tract`, `mask_file`, `n_streamlines`.
- `tracts/<tract>.nii.gz`: binary voxelisation of the tract on the 2 mm working grid.
- `streamlines.npz`: `points` float32 (N, 3) concatenated streamlines; `offsets` int64
  (S + 1), streamline s is `points[offsets[s]:offsets[s+1]]`; `tract_index` int16 (S)
  indexes `tract_names` (sorted tract names).

Tract disconnection of a lesion = fraction of a tract's streamlines with at least one
point inside the lesion mask.
