# cognet-stroke: COGNET-STROKE cognitive disconnection profile

Pipeline slot `cognet-profile`. Turns a lesion mask and a functional disconnection map (both on the
2 mm MNI152NLin6Asym working grid) into a cognitive disconnection profile: a score and a
permutation p-value per concept and per domain. Manifest: [`step.yaml`](step.yaml) (contract 1.2).

## Where the code lives

This wrapper is platform code and lives here, not in the author's repository. The
[COGNET-STROKE](https://github.com/compneurobilbao/COGNET-STROKE) repository is consumed
**unmodified** at the commit pinned in [`upstream.env`](upstream.env) (the registry entry repeats
it as `source.upstream`; `pipelines/stroke-lnm/registry.py check` keeps the two equal): the
[`Dockerfile`](Dockerfile) copies its `src/cdr.py` byte for byte to
`/opt/upstream/COGNET-STROKE/src/cdr.py`, and the wrapper imports it from there
(`src/cognet_stroke_step/upstream.py`) and calls its `compute_cdr` and `compute_overlap`. Nothing
of the author's analysis environment (concept-map generation, brainsmash, neuromaps, nilearn,
pandas) is in the image: only numpy and nibabel (versions of the author's lock file), matplotlib
for the figure, and the SDK.

## What it computes

1. Inputs: an empty lesion is rejected (`mask_empty`); infinite values in the map are rejected
   (`disconnection_not_finite`); a map without any defined (non-NaN) value, or without one in any
   parcel, is rejected (`disconnection_map_undefined`); a map that is zero wherever it is defined
   fails QC (`disconnection_map_empty`).
2. The positive part and the magnitude of the negative part of the map, and the lesion, are
   reduced to the parcels of the bundle's parcellation (mean of the defined voxels per parcel; a
   parcel counts as lesioned if any voxel is). NaN voxels stay undefined in both parts (the
   author's workflow turns them into zeros before parcellating; the wrapper does not, so that an
   undefined parcel is not read as "no disconnection"). A parcel without any defined voxel is NaN.
3. For every concept of the bundle: the author's `compute_cdr` (mean inside the concept divided
   by the mean outside, with `nanmean`, so NaN parcels are left out of both means for the concept
   and for every null mask; p = fraction of null masks with a strictly larger value, at least
   1 / n_nulls) for both parts, and the author's `compute_overlap` for the lesion.
4. Around those calls, the wrapper adds: `flag: empty_mask` with a null score and a null p-value
   when the score is undefined (no defined parcel inside or outside the concept; the author's code
   returns NaN with p = 1 / n_nulls there, which would read as the strongest result), and
   `flag: zero_outside_mean` when the defined mean outside the concept is zero (the author's code
   then returns the inside mean). Bonferroni over concepts per measure; a domain's score is the
   mean of its concepts' finite scores, its p-value the smallest concept p-value times their
   number, Bonferroni over domains.
5. When parcels had no defined value, the profile carries
   `excluded_parcels: {count, fraction, reason: undefined_in_disconnection_map}` (schema
   `contracts/schemas/v1/cognet-profile.schema.json`), the provenance a warning
   `parcels_excluded`, and the figure a footnote. The research report states it.

**Changes from 1.0.0 (1.1.0, minor).** 1.0.0 was built from a `step/` folder of the author's
repository and used its own re-implementation of the CDR. 1.1.0 calls the author's code; on maps
without NaN it gives the same profile (CI compares it with the published 1.0.0 image on the
reference case; only `profile_version` changes, `1.0` to `1.1`, because profiles may now carry
`excluded_parcels`). New: NaN accepted (1.0.0 rejected any NaN with `disconnection_not_finite`,
which now means infinite values only), the error code `disconnection_map_undefined`, the warning
`parcels_excluded`, and a matplotlib-only figure. Ports, parameters and the bundle layout are
unchanged; no error code was removed.

## Bundle

Type `concept-maps`, mounted at `/ref/concept_maps`; layout and how to build it from the author's
workflow outputs (concept maps and null-map cache) with [`bundle/build_bundle.py`](bundle/build_bundle.py):
[docs/reference-data.md](../../docs/reference-data.md#the-concept-maps-bundle). The parcellation
and therefore the parcel count come from the bundle (the author now uses `firsT_2566`, 2566
parcels); the step fixes none.

## Tests and CI

- `tests/test_cognet_stroke.py`: the reference case against an independent naive implementation;
  every score and p-value equal to a direct call of the author's functions; the same numbers as
  1.0.0's implementation on finite vectors; NaN parcels (excluded, reported, and scores equal to
  the naive computation without those parcels), partial NaN parcels, the all-NaN and infinite
  rejections, a bundle with another parcel count, and the other rejections. The tests that need
  the author's code read it from `$COGNET_STROKE_SRC` (`steps/fetch-upstream.sh steps/cognet-stroke DEST`).
- `tests/test_build_bundle.py`: the bundle builder on synthetic workflow outputs.
- Job `cognet-stroke-container` of [`.github/workflows/steps.yml`](../../.github/workflows/steps.yml):
  the tests with the author's code at the pinned commit, the image (size, no analysis packages,
  the author's file byte for byte), the reference test under Docker and Apptainer, NaN and
  rejection cases in the container, and the equivalence with the published 1.0.0 image.

Local image: `docker build -f steps/cognet-stroke/Dockerfile -t cognet-stroke-step:dev .` from the
repository root.
