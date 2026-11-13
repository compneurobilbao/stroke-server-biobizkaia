# Synthetic fixtures

Deterministic, fully synthetic test data for CI, unit tests and demos. No patient
data and no third-party data are used or redistributed.

## Regenerate

```
pip install -r backend/requirements.txt
python -m fixtures.generate --out fixtures/data      # or any other directory
```

Seeds are fixed (`fixtures/generate.py`, `SEED`). Two runs give byte-identical output.
Across platforms the gzip bytes may differ with the zlib build, so reproducibility is
asserted on the decompressed payload (`payload_sha256` in `fixtures/data/CHECKSUMS.json`).

## Contents of `fixtures/data/` (about 1.5 MB)

| Path | Artefact type | Notes |
|---|---|---|
| `cases/case-01`, `cases/case-02` | `ct_native`, `lesion_mask_native`, `lesion_mask_mni` | Synthetic "patient" CT (76x92x60 voxels, 2.5x2.5x3 mm, HU, int16), its native lesion mask, and the exact MNI ground-truth mask on the working grid. `truth.json` holds the random affine, deformation and lesion parameters. |
| `mni_masks/*.nii.gz` | `lesion_mask_mni` | Seven masks on `MNI152NLin6Asym-res2` (1 to 90 mL, left and right, one two-part blob). `index.tsv` lists side and volume. |
| `bundles/toy-normative-connectome/1.0.0` | bundle type `normative-connectome` | Synthetic parcellation (100 parcels), random low-rank connectivity for 20 synthetic participants with sex and age band. Placeholder layout (`placeholder-v0`), documented in the bundle `README.md`, because the flnm-fast input format is not yet known. |
| `bundles/toy-tract-atlas/1.0.0` | bundle type `tract-atlas` | Four synthetic tracts as streamlines (`streamlines.npz`) plus voxel masks on the working grid. Placeholder layout. |

Both bundles have `synthetic: true`, licence CC0-1.0 and `redistributable: true`.

## How the CT is made

The "public CT template" route was not taken (see `LICENCES.md`). The head is an
analytic phantom (scalp, skull, grey and white matter, ventricles) defined as a
closed-form function of MNI coordinates in `fixtures/phantom.py`. A patient is the
phantom sampled on a native CT grid through a random rotation, scaling and shift plus a
smooth sinusoidal deformation, with Gaussian noise. The lesion is an ellipsoid in MNI
space lowered by 14 HU inside the brain; native and MNI masks therefore agree exactly
with the geometry. The phantom is anatomically crude on purpose: it supports plumbing,
contract and QC-logic tests, not registration accuracy benchmarks.

## Tests

`tests/test_fixtures.py` checks that regeneration reproduces the committed checksums,
that committed files match `CHECKSUMS.json`, that masks are binary and on the grid of
the artefact-type registry, and that the bundle manifests pass `contracts/validate.py`
and match the files on disk.
