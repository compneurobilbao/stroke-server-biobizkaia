# flnm-fast

Pipeline slot `functional-disconnection`: functional lesion network z-map of one lesion, from a normative functional connectivity bundle.

flnm-fast has no source repository. Its deliverable is the public image `compneurobilbaolab/flnm-fast:2.1.0`, pinned here by digest (`sha256:eb3f88383d1594fa4ca8ec3c2b1c4207a13b8a46b391cf8c12d3cda196f40d9b`, labels: title `flnm-fast`, "Create lesion network z-maps", version `2.1.0`, licence MIT; the image's own entry point is `uv run --no-project --no-config python /opt/lesion_zmaps/create_lesion_zmaps.py`, which this step clears and replaces by the contract entry point, running the script with the image's Python). This step is a **wrapper**: the [`Dockerfile`](Dockerfile) starts `FROM` that digest, adds the SDK, the manifest `/opt/step/step.yaml` and the contract entry point `/usr/local/bin/run`, and changes nothing else (a few MB on top of the base, reported by the CI job; Python, numpy, nibabel, pandas and scipy are the engine's own). The engine script is run unchanged, as a subprocess, once per run.

Versions 2.0.0 and 3.0.0 are the worked example of an external tool ([steps/README.md](../README.md#wrapping-an-external-tool)): it declares contract 1.2 and the `upstream` block (engine name, version `2.1.0`, homepage, image digest equal to the `FROM` of the `Dockerfile`, licence), and its registry entries (`pipelines/stroke-lnm/registry.yaml`, one per pinned cohort bundle) have `origin: external`, a licence review (pending confirmation by the image author) and the validation status `reference-tested`.

**Changes from 2.0.0 (3.0.0, major).** The normative choice is the cohort only: the step no longer matches controls by sex and age. (1) The parameters `sex`, `age_band` and `control_count` are removed (the step has no parameters; a run that still passes one is refused with the reserved code `invalid_parameters`), so the pipeline binds nothing from the case. (2) The rejections `participant_sex_missing`, `participant_age_missing` and `insufficient_controls` are removed: they existed only for the matching. (3) The wrapper no longer passes `--match-age-sex` (engine 2.1.0 then uses every participant of the mounted normative table and ignores `--control-count`, which is why that parameter went away; the engine reads only the `id` column of the one-row participants table, so no age or sex is written either). (4) The parcellation file is read from the bundle instead of a fixed name ([below](#the-normative-fc-bundle)). (5) The wrapper checks, before running the engine, that `normative_subjects.csv` has exactly one row for every participant of the connectivity array (what the engine requires without matching) and reports a mismatch as `bundle_layout_unsupported`. Age- or sex-matched normative cohorts may come in a future version; they would be a new parameter or a new bundle layout, not a change of these semantics. Which cohort is used is chosen by the pipeline: `stroke-lnm` 0.6.0 offers the same step image as `flnm-fast-hcp-aging` (default) and `flnm-fast-hcp-ya`, each pinning one bundle, plus the CI-only `flnm-fast-synthetic`.

**Changes from 1.1.0 (engine 1.0 to 2.1.0).** Inputs, outputs, parameters, bundle layout and the other error codes are those of 1.x, with three differences. (1) Engine 2.1.0 weights every lesion-overlapped parcel; engine 1.0 left the lowest-numbered one out of the weighting, so the z-map of the same case and cohort differs from 1.x (see "Engine overlap behaviour" below). (2) The 1.x warning `lowest_overlapped_parcel_omitted` and the rejection `lesion_single_parcel` are gone: a lesion within one parcel now yields a map (weight 1 on that parcel). Removing a declared error code is a major version change ([versioning.md](../../docs/contracts/versioning.md#3-steps)), hence 2.0.0. (3) Engine 2.x selects controls by sex and age only with its flag `--match-age-sex` (1.0 always did; without the flag 2.x uses every participant and ignores `control_count`); 2.0.0 passed the flag; 3.0.0 does not (see above).

## What it computes (engine)

For one lesion: the parcels of the normative parcellation overlapped by the lesion get weights proportional to their overlap voxels; each participant's lesion-weighted connectivity profile over all target parcels is formed for **every participant of the mounted cohort** (no sex or age matching, no subset); every target parcel is tested against zero across participants (one-sample t-test), the signed t is converted to a signed z (`sign(t) * isf(p / 2)`), and the z of each parcel is written on that parcel's voxels. Output type `fdisconnection_map`: float32 on the 2 mm MNI152NLin6Asym grid, zero outside the parcellation, NaN for a parcel whose statistic is undefined (fewer than two finite connectivity values across the participants; reported as warning `undefined_parcels`). A parcel whose connectivity is exactly constant across the participants is not undefined in 2.1.0: rounding in its regression leaves a tiny non-zero variance and the parcel gets a very large finite z (about 32.5), where 1.0 gave NaN; real connectivity is never constant, so this is recorded, not worked around. The sign is that of the lesion-to-parcel connectivity (positive: positively coupled with the lesion site), not "more disconnection": the artefact type text in `contracts/artefact-types.yaml` speaks of "larger values meaning more disconnection" and should be revisited by a contract change (see the PR).

## Contract mapping

| Contract | Engine |
|---|---|
| input `lesion_mask` (`lesion_mask_mni`) | `--lesion-dir` with `case.nii.gz` (copy in `/tmp`) |
| parameters | none; `participants.tsv` has only the `id` column (`case`), and neither `--match-age-sex` nor `--control-count` is passed |
| bundle `normative` (type `normative-fc`) at `/ref/normative` | `--fc`, `--normative-subjects`, `--parcellation` (file named by the bundle) |
| `case_zmap.nii.gz` | output `fdisconnection_map` |

The engine's other files (`*_controls.csv` lists the normative participants' identifiers, `*_overlap.csv`, `*_roi_z.csv`) are kept in `/tmp` and discarded; only counts reach the log. Nothing is written to `/out/extra`.

The case's sex and age band are not used by this step.

## The `normative-fc` bundle

A reference bundle ([ADR-0006](../../docs/adr/0006-reference-bundles-are-data.md), [`docs/reference-data.md`](../../docs/reference-data.md)) of type `normative-fc`, mounted at `/ref/normative`:

```
bundle.yaml                         # id, version, type: normative-fc, licence, files + hashes; metadata.parcellation (optional, see below)
fc_row_subject_col.npy              # float, shape (n_roi, n_subject, n_roi); FC[roi, subject, roi]
normative_subjects.csv              # column subject_index (position on the subject axis), one row per participant; other columns ignored
standard/<parcellation>.nii.gz      # integer parcellation, labels 1..n_roi, on the 2 mm MNI152NLin6Asym grid
```

Required: the array, the table with a `subject_index` column that covers 0..n_subject-1 exactly once each, and one parcellation. **No age or sex columns are needed** (the real HCP tables have none): `subject_id`, `source_file`, site and framewise-displacement columns are accepted and ignored. A cohort is one bundle (for example `normative-fc-hcp-aging`, `normative-fc-hcp-ya`); the step uses all of its participants.

**Parcellation rule** (`parcellation_path` in the step; the step never guesses between files): (1) if `bundle.yaml` has `metadata.parcellation`, it is a path relative to the bundle root, for example `standard/firsT_2566.nii.gz`, and must name an existing `.nii.gz` file inside the bundle; (2) otherwise the bundle must contain exactly one `*.nii.gz` file directly under `standard/`, which is used. Zero or several candidates without the metadata, a missing or non-`.nii.gz` file, or a path leaving the bundle give `bundle_layout_unsupported`. The parcellation must be on the working grid (91 x 109 x 91, 2 mm). The file names are those of the lab's local normative data directories (`fc_row_subject_col.npy`, `normative_subjects.csv`, `standard/`); the directories' `participants.tsv`, `lesion/` and `zmaps/` are the lab's own case data and outputs and must not be in a bundle (the platform supplies the case). The engine's checks on the table are listed in [reference-data.md](../../docs/reference-data.md#the-normative-fc-bundle).

**Licence constraint.** The normative connectivity data is derived from open-access databases (such as the Human Connectome Project) whose data use terms forbid redistribution. It is never downloaded by CI or agents, never committed, never baked into an image and never put in a release tarball or on-premise installer: the bundle is created and registered locally from the lab's copy, with `licence.redistributable: false` (`licence.spdx: LicenseRef-<name>` and the terms URL in `licence.terms_url`), and mounted read-only at run time. CI and the tests use only the synthetic bundle below.

## Tests and reference data

`tests/make_reference.py` writes (deterministically, nothing committed, `tests/reference/` is git-ignored) a synthetic `normative-fc` bundle (27 block parcels, 200 participants with identifiers only, no age or sex; integer-hash connectivity, under 1 MB; `bundle.yaml` names the parcellation `standard/synthetic_blocks27_2mm.nii.gz` in `metadata.parcellation`), a lesion straddling 8 parcels, and the expected z-map computed with the standard library and numpy only (exact t and normal quantile functions). It is the manifest's `reference_test.generator`; the bundle is passed to the runner with `--bundle normative=steps/flnm-fast/tests/reference/bundle`, and the stroke-lnm end-to-end test registers the same bundle.

```sh
python -m pytest steps/flnm-fast            # wrapper tests; engine tests are skipped without the engine
# with the engine (needs pandas and scipy; extract the script from the pinned image):
docker run --rm --entrypoint cat compneurobilbaolab/flnm-fast@sha256:eb3f88383d1594fa4ca8ec3c2b1c4207a13b8a46b391cf8c12d3cda196f40d9b \
    /opt/lesion_zmaps/create_lesion_zmaps.py > /tmp/create_lesion_zmaps.py
FLNM_FAST_SCRIPT=/tmp/create_lesion_zmaps.py python -m pytest steps/flnm-fast
python steps/flnm-fast/tests/make_reference.py
docker build -f steps/flnm-fast/Dockerfile -t flnm-fast-step:dev .
stroke-step-reftest --step-dir steps/flnm-fast --mode docker --image flnm-fast-step:dev \
    --bundle normative=steps/flnm-fast/tests/reference/bundle
```

Rejections (exit 10, declared in `step.yaml`; the reserved codes of contract 1.1 come from the SDK): `mask_empty`, `lesion_outside_parcellation`. Internal failures (exit 1): `bundle_layout_unsupported` (missing file, parcellation not identifiable or not on the working grid, subject table not covering the array), `engine_failed`, `engine_output_missing`.

## Engine overlap behaviour (fixed upstream; the 1.x workaround was removed)

Engine 1.0 computed the overlap weights in `compute_roi_overlap_weights` with `roi_labels = np.flatnonzero(counts)[1:]` after the background label had already been excluded, so on every run the lowest-numbered lesion-overlapped parcel was left out of the weighting (the other weights were renormalised) and a lesion within a single parcel left nothing to weight (all-NaN map, exit 0). Engine 2.1.0 uses `roi_labels = np.flatnonzero(counts)` and weights every overlapped parcel. Checked on the code of the pinned digest and with a synthetic test (three parcels; lesions in parcels 1-3, 2-3 and 2 only): 1.0 returned parcels {2, 3}, {3} and none, 2.1.0 returns {1, 2, 3}, {2, 3} and {2}. In this step the same property is pinned by `test_lowest_overlapped_parcel_is_part_of_the_weighting` and `test_lesion_in_a_single_parcel_is_processed`, which compare the real engine with the independent computation of `tests/make_reference.py`. Step 1.x had a workaround (warning `lowest_overlapped_parcel_omitted`, rejection `lesion_single_parcel`); it was removed in 2.0.0.
