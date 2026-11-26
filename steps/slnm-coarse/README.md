# slnm-coarse

Parcel-level structural lesion network mapping (SLNM) of one lesion on a normative diffusion cohort. Step 1.0.0, contract 1.2, `provides: structural-disconnection`.

**Status: in development.** The step, its image recipe, its tests and its CI job exist, but it is not part of any pipeline yet: `stroke-lnm` lists it as an in-development stage (greyed, never selectable; `in_development` in [`registry.yaml`](../../pipelines/stroke-lnm/registry.yaml)), no profile enables it, no image is published and the real cohort cache bundle is not built. See [Open points](#open-points).

This step is a **wrapper** of the `slnm-coarse` command of the author's [fast-slnm](https://github.com/compneurobilbao/fast-slnm) repository (MIT), consumed unmodified at the commit pinned in [`upstream.env`](upstream.env) (the STEPS-01 pattern, [steps/README.md](../README.md#wrapping-a-tool-of-an-authors-repository)). Only the coarse workflow is used; the author's indexed-TDI workflow (voxel-wise, needs the original tractograms) is out of scope here.

## What it computes

Per participant of the normative cohort (the author's `docs/coarse.md` at the pinned commit):

1. the lesion (on the common atlas grid) is warped to the participant's native diffusion space with the cached forward ANTs transforms (`antsApplyTransforms`, nearest neighbour, one thread);
2. for each parcel `r` of the participant's native atlas, the fraction `p[r]` of its voxels inside the warped lesion is taken as the probability that a streamline visiting `r` is hit there;
3. a streamline that visits parcels `R` is hit with probability `1 - prod_{r in R} (1 - p[r])` (contacts treated as independent);
4. the participant's score of parcel `r` is the SIFT2-weighted mean hit probability of the streamlines that visit `r`; the participant supports `r` when its normative SIFT2 throughput through `r` is non-zero.

Across participants, each parcel's group value is the equal-weight mean of the four acquisition-site means (MGH, UCLA, UMinn, WashU) over supporting participants. A parcel is **valid** only if all four sites contribute and more than four participants support it in total. The value is painted on the parcel's voxels of the common atlas grid.

### Approximation caveats (from the author's documentation)

- It is a **parcel-level approximation**, not an exact streamline-lesion intersection and not a voxel-wise track-density fraction; it is not equivalent to filtering every streamline. The author describes the exposure model as a spatially coarse, unvalidated probabilistic heuristic.
- The lesion is modelled only where it overlaps the native atlas: the part of a warped lesion outside the atlas is not used (warning `lesion_outside_native_atlas`).
- Registrations are the cohort's own; they should be visually checked before results are interpreted.
- The group value weights sites equally; the raw (cohort-weighted) statistics differ.

## Contract mapping

| Contract | slnm-coarse |
|---|---|
| input `lesion_mask` (`lesion_mask_mni`, working grid) | `--lesion` (`/tmp/.../lesion.nii.gz`: the same voxels as uint8 with the header of the cache atlas) |
| bundle `cohort_cache` (`slnm-coarse-cache`) at `/ref/cohort_cache` | `--cache-root cache/`, `--participants participants.csv` |
| parameter `workers` (default 2, at most the granted CPUs) | `--workers` |
| `lesion_slnm.nii.gz` | output `sdisconnection_map` (zero on unsupported parcels replaced by NaN) |
| `valid_mask.nii.gz` | output `sdisconnection_valid_mask` (uint8) |
| `support_count.nii.gz`, `roi_statistics.csv`, `summary.json` | `/out/extra/` (the summary without paths, with parcel counts and aggregate participant QC) |

**Grid.** `slnm-coarse` requires the lesion to have exactly the shape and affine of the common atlas (`firsT_2566_MWMA400_min20`, 2 mm, 91 x 109 x 91). The wrapper checks that the atlas of the bundle is on the platform's working grid `MNI152NLin6Asym-res2` (shape, affine within 1e-4 mm, as `flnm-fast` checks its parcellation) and that its labels are integers 0 to 2966 with 2966 present; the lesion, already on the working grid, is then written with the atlas header. Nothing is resampled. An atlas on another grid makes the step fail with `bundle_layout_unsupported`.

**Unsupported parcels are never zeros.** `slnm-coarse` stores zero for parcels without a valid group estimate. The wrapper writes NaN there (zero stays only outside the atlas), publishes the validity mask as a second output, and records the warning `unsupported_parcels` with the count. A one-participant or single-site cohort cannot give a valid parcel; the wrapper refuses such a bundle before running (`cohort_insufficient`).

**Participant data.** The participant-level files of `slnm-coarse` (scores, checkpoints, `participant_qc.csv` with the normative participants' identifiers) stay in `/tmp` and are discarded; only aggregate counts reach the provenance and `extra/slnm_coarse_summary.json`. Error details have paths and participant identifiers removed.

### Errors and warnings

| Code | Category | When |
|---|---|---|
| `mask_empty` | input rejected | the lesion has no voxel |
| `lesion_outside_atlas` | input rejected | the lesion has no voxel in any parcel of the atlas |
| `bundle_layout_unsupported` | internal | atlas missing or off the working grid or with wrong labels; table without `subject_label`/`site` or with an unknown site; a participant without a cache directory |
| `cohort_insufficient` | internal | a site missing from the table, or at most four participants |
| `slnm_coarse_failed` | internal | `slnm-coarse` exited with an error (its last line, scrubbed, in `details`) |
| `slnm_coarse_output_missing`, `slnm_coarse_output_invalid` | internal | expected output missing, off the grid, outside [0, 1] or non-zero outside the valid mask |
| warning `unsupported_parcels` | | parcels of the atlas without a valid group estimate (NaN in the map) |
| warning `lesion_lost_in_native_space` | | participants for whom the warped lesion has no voxel |
| warning `lesion_outside_native_atlas` | | participants for whom part of the warped lesion is outside the native atlas |

## The `slnm-coarse-cache` bundle

Mounted read-only at `/ref/cohort_cache`. Layout (`metadata.layout: slnm-coarse-cache-v1`):

```
bundle.yaml
participants.csv                                     # subject_label (without _DiffusionRecommended), site
cache/firsT_2566_MWMA400_min20.nii.gz                # the common atlas, working grid, labels up to 2966 (gaps kept)
cache/<subject_label>_DiffusionRecommended/
    metadata.json                                    # participant, streamlines, inputs_mode: copy, atlas fingerprint
    atlas_dwispace.nii.gz                            # native atlas
    index/roi_offsets.npy, index/track_ids.npy, index/roi_weight_totals.npy
    dwi_bzero.nii.gz                                 # native reference grid
    dwireg/standard2dwi0GenericAffine.mat, dwireg/standard2dwi1Warp.nii.gz   # forward transforms
    streamlines/sift2_weights.txt
```

No tractogram and no inverse warp. The real bundle is derived from HCP Aging diffusion data: `licence.redistributable: false`, built and registered on the server only, never committed, never in an image or CI. How to build it from the author's `coarse_files` cache with [`bundle/build_bundle.py`](bundle/build_bundle.py): [docs/reference-data.md](../../docs/reference-data.md#the-slnm-coarse-cache-bundle).

## Resources

`cpus: 2`, `memory_mb: 8192`, `wall_time_minutes: 240`, `scratch_mb: 2048` (provisional). Each worker is one process running one single-threaded `antsApplyTransforms` and holding one participant's index (memory-mapped track identifiers, SIFT2 weights); `workers` (default 2, capped at the CPUs the executor grants) leaves room for the rest of a 4-core host. Wall time per lesion on the real cohort has not been measured (open point).

## Image

[`Dockerfile`](Dockerfile), after the author's own: Ubuntu 24.04 (pinned by digest) with its Python 3.12, `antsApplyTransforms` from the ANTs 2.6.3 release binary (SHA-256 checked; ANTs licence files in `/usr/share/doc/ants`), and the `slnm-fast` package installed with uv 0.4.30 (pinned by digest) from the author's `uv.lock` (`uv sync --locked --no-dev`), into `/opt/venv`. MRtrix3 is not installed (the coarse workflow does not use it). The SDK and its pinned requirements ([`requirements-step.txt`](requirements-step.txt)) are added to the same environment, and `uv pip check` must pass. `/opt/upstream/fast-slnm/` holds the author's `LICENSE` and `UPSTREAM_COMMIT`.

The author's repository is **private** until its first public release. The archive is then read through the GitHub API with a BuildKit secret (not stored in any layer): `docker build --secret id=upstream_token,env=UPSTREAM_TOKEN -f steps/slnm-coarse/Dockerfile .`, with a token that can read the repository (for example `UPSTREAM_TOKEN=$(gh auth token)`). Once public, no secret is needed. `steps/fetch-upstream.sh` uses `UPSTREAM_TOKEN` (or `GITHUB_TOKEN`) the same way and exits 3 when the repository is not readable.

## Tests and CI

- [`tests/make_reference.py`](tests/make_reference.py) writes a fully synthetic cohort cache bundle (nine participants over the four sites, eight block parcels with labels up to 2966, native grids that are crops of the working grid, identity transforms written as an ITK affine and a zero displacement field, hash-generated streamline-to-parcel incidence and SIFT2 weights; parcel 2900 lacks support at one site and parcel 2966 has no streamline), a ball lesion and the expected map and mask, computed with numpy from the definition of the method.
- [`tests/test_slnm_coarse.py`](tests/test_slnm_coarse.py): manifest, rejections and bundle failures, scrubbing of errors, the NaN rule, the worker cap; with the author's code (`SLNM_FAST_SRC`): the constants of the wrapper equal the author's, the independent expected values equal the author's functions on the same cache, and the whole step on the reference case (antsApplyTransforms replaced on the host by an identity nearest-neighbour fake). [`tests/test_slnm_coarse_bundle.py`](tests/test_slnm_coarse_bundle.py): the bundle builder.
- CI job `slnm-coarse-container` (`.github/workflows/steps.yml`): fetches the author's code (content digest checked, a wrong digest must fail), runs the tests with it, builds the image, checks that the installed package is the author's source byte for byte, runs the reference test with the real ANTs under Docker and Apptainer (identical outputs) and the rejection path. While the repository is private and no read token is configured (`FAST_SLNM_READ_TOKEN` secret), the fetch reports "not readable" and these steps are **skipped** with a warning and a job-summary note; any other fetch failure fails the job.

```sh
UPSTREAM_TOKEN=$(gh auth token) steps/fetch-upstream.sh steps/slnm-coarse /tmp/fast-slnm
SLNM_FAST_SRC=/tmp/fast-slnm python -m pytest steps/slnm-coarse
python steps/slnm-coarse/tests/make_reference.py
UPSTREAM_TOKEN=$(gh auth token) docker build --secret id=upstream_token,env=UPSTREAM_TOKEN \
    -f steps/slnm-coarse/Dockerfile -t slnm-coarse-step:dev .
stroke-step-reftest --step-dir steps/slnm-coarse --mode docker --image slnm-coarse-step:dev \
    --bundle cohort_cache=steps/slnm-coarse/tests/reference/bundle
```

## Licence and validation

Wrapper: MIT (this repository). fast-slnm: MIT (its `LICENSE` is in the image). ANTs: Apache-2.0. Validation status when the step gets an implementation entry: at most `reference-tested` (the synthetic reference test above); `licence_review` to record MIT for the code (pending confirmation by the responsible person) and that the cohort cache is a non-redistributable bundle, never in the image.

## Open points

- The real cohort cache arrives later; the bundle is not built and its size is not known.
- Time and memory per lesion on the real cohort are unknown; `resources` and the `workers` default are provisional.
- The author's repository is private: CI skips the image build and reference tests until it is public or a read token is configured.
- Wiring as an optional pipeline slot (new slot, implementation entry with licence review and validation, pipeline minor version, report and viewer support for the new map) is a later card.
