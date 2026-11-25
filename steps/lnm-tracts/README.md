# lnm-tracts: tract-wise structural disconnection step

Pipeline slot `tract-disconnection`. Contract step (step contract 1.0) that ports the author's
`src/lnm_tracts.sh` of the [lnm](https://github.com/compneurobilbao/lnm) repository: for every
tract of an atlas, count the streamlines, remove those passing through the lesion with
`tckedit -exclude`, count again. Voxel-wise structural disconnection (`src/lnm_structural.sh`)
is a different step.

## Where the code lives

This step is platform code and lives here, not in the author's repository. It is a **port**:
the algorithm of `src/lnm_tracts.sh` re-written in Python (`src/lnm_tracts_step`), and the
released image contains no code of the lnm repository. `upstream` ([`upstream.env`](upstream.env),
repeated as `source.upstream` in the registry entry and kept equal by
`pipelines/stroke-lnm/registry.py check`) only names the commit whose unmodified
`src/lnm_tracts.sh` CI runs on the same input to compare: the CI-only `legacy` target of the
[`Dockerfile`](Dockerfile), never released, holds that script (content checked against
`UPSTREAM_SHA256`).

Version 1.0.1 is version 1.0.0 (previously built from the `step/` folder of the lnm repository at
that same commit) moved into this repository: same code, same manifest apart from the version,
`source_url` and description, same results. It is a new version only because the image is
rebuilt (a step version identifies one image). Version 1.1.0 adds the atlas-space resampling
below, in the wrapper only: the pinned commit and its `src/lnm_tracts.sh` are unchanged. For an
atlas without `metadata.space` the tables are those of 1.0.x.

## Contract mapping

- Input `lesion_mask`: binary mask on the 2 mm MNI152NLin6Asym working grid.
- Output `tract_disconnection` (`tract_disconnection.tsv`): columns `tract`, `disconnection`,
  `n_streamlines`, `n_disconnected`. `disconnection` is the fraction of streamlines removed, which
  is `1 - Disc` of the script. The script printed `Disc` truncated to three decimals by `bc`;
  the step computes the fraction from the integer counts, so the two agree to within 0.001.
- Bundle `tract_atlas` (type `tract-atlas`), mounted at `/ref/tract_atlas`: `labels.txt` (one tract
  name per line) and one `<tract name>.tck` per name, in the millimetre coordinates of the space
  its `metadata.space` declares (MNI152NLin6Asym when absent; see "Atlas space" below and
  [docs/reference-data.md](../../docs/reference-data.md#the-tract-atlas-bundle)). This is the
  layout of the former `yeh_tracts` directory, so the HCP1065 atlas needs no conversion of its
  files.
- Optional bundle `space_transform` (type `space-transform`), mounted at `/ref/space_transform`:
  see "Atlas space" below.
- The atlas is read-only; all scratch is in `/tmp`, so any number of cases can run at once on the
  same atlas.

## Atlas space (1.1.0)

The lesion mask is in MNI152NLin6Asym. A tract atlas bundle may declare the space of its
streamline coordinates in `metadata.space` of its `bundle.yaml`:

- no `metadata.space`, or `MNI152NLin6Asym`: the lesion is used as it is, exactly as in 1.0.x
  (a mounted `space_transform` bundle is then ignored);
- any other space (for example `MNI152NLin2009aAsym` for the HCP1065 atlas): the lesion is first
  resampled into the atlas space with the bundle `space_transform`. Without it the step stops
  with the internal failure `space_transform_missing`; a transform for another pair of spaces
  gives `space_transform_mismatch`.

Layout of a `space-transform` bundle: `bundle.yaml` with `metadata.from_space`
(`MNI152NLin6Asym`) and `metadata.to_space` (the atlas space), and `coordinate_map.nii.gz`, a
float image of shape (X, Y, Z, 3) on a grid of the atlas space whose voxel (i, j, k) holds the
RAS millimetre position in MNI152NLin6Asym of its centre (the image-resampling direction that
ANTs and TemplateFlow call `mode-image`). The step needs only nibabel and numpy to apply it.
The bundle for the HCP1065 atlas is built by
[`scripts/bundles/space_transform/build.sh`](../../scripts/bundles/space_transform/build.sh)
([docs/reference-data.md](../../docs/reference-data.md#the-space-transform-bundle)).

Resampling is nearest neighbour onto the grid of the coordinate map. The lesion is a binary
label: nearest neighbour introduces no new values and needs no threshold, keeps the volume
(2 mm to 1 mm on the same field of view maps every voxel to exactly eight) and keeps small or
thin lesions, which linear interpolation followed by a 0.5 threshold erodes (with
`tckedit -exclude` any streamline that touches the lesion counts). Voxel counts and volumes
before and after are written to the provenance warning `lesion_resampled_to_atlas_space` and to
`extra/atlas_space_resampling.json`; the resampled mask is kept as
`extra/lesion_mask_atlas_space.nii.gz`. A lesion with no voxel left on the atlas grid is rejected
with `mask_outside_atlas_space`. The output table has the same format as before.

## Tests and CI

Tests and CI use only synthetic data generated by `tests/make_reference.py` (a toy atlas and four
lesions, with expected values computed in numpy; nothing is committed). The job `lnm-tracts` of
[`.github/workflows/steps.yml`](../../.github/workflows/steps.yml):

- runs `tests/` (the MRtrix-dependent tests only where `tckedit` is on PATH);
- builds the image and the legacy image, runs the reference test under Docker and Apptainer,
  all lesions at once on one read-only atlas mount (`ci/run_cases.sh`) against the numpy oracle
  (`ci/compare_oracle.py`), the unmodified `src/lnm_tracts.sh` on the same input
  (`ci/compare_legacy.py`), and the `mask_empty` rejection;
- **atlas space** (`ci/run_space_cases.sh`, synthetic spaces only, no real template): the toy
  atlas declaring `ToyAtlasSpace` with an identity transform (tables byte-identical to the runs
  without a transform) and a 4 mm shift (tables equal to the numpy oracle on lesions moved by
  4 mm), and the `space_transform_missing` and `space_transform_mismatch` failures;
- **equivalence with 1.0.1**: runs the previously published image (`step-lnm-tracts` 1.0.1, by
  digest) on every lesion and requires byte-identical tables.

Local image: `docker build -f steps/lnm-tracts/Dockerfile -t lnm-tracts-step:dev .` from the
repository root.
