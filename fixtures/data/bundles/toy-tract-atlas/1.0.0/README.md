# Toy tract atlas (synthetic)

Four synthetic tracts, defined as bundles of curved streamlines in MNI152NLin6Asym
millimetre coordinates, in the layout read by the tract-wise disconnection step
(`lnm-tracts`):

- `labels.txt`: one tract name per line (the order of the output table);
- `<tract name>.tck`: the streamlines of that tract (MRtrix3 format, float32), 60 each.

Tract disconnection of a lesion = fraction of a tract's streamlines with at least one
vertex inside the lesion mask.
