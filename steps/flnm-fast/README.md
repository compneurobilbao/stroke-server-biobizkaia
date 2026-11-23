# flnm-fast

Pipeline slot `functional-disconnection`: functional lesion network z-map of one lesion, from a normative functional connectivity bundle.

flnm-fast has no source repository. Its deliverable is the public image `compneurobilbaolab/flnm-fast:2.1.0`, pinned here by digest (`sha256:eb3f88383d1594fa4ca8ec3c2b1c4207a13b8a46b391cf8c12d3cda196f40d9b`, labels: title `flnm-fast`, "Create lesion network z-maps", version `2.1.0`, licence MIT; the image's own entry point is `uv run --no-project --no-config python /opt/lesion_zmaps/create_lesion_zmaps.py`, which this step clears and replaces by the contract entry point, running the script with the image's Python). This step is a **wrapper**: the [`Dockerfile`](Dockerfile) starts `FROM` that digest, adds the SDK, the manifest `/opt/step/step.yaml` and the contract entry point `/usr/local/bin/run`, and changes nothing else (a few MB on top of the base, reported by the CI job; Python, numpy, nibabel, pandas and scipy are the engine's own). The engine script is run unchanged, as a subprocess, once per run.

Version 2.0.0 is the worked example of an external tool ([steps/README.md](../README.md#wrapping-an-external-tool)): it declares contract 1.2 and the `upstream` block (engine name, version `2.1.0`, homepage, image digest equal to the `FROM` of the `Dockerfile`, licence), and its registry entry (`pipelines/stroke-lnm/registry.yaml`) has `origin: external`, a licence review (pending confirmation by the image author) and the validation status `reference-tested`.

**Changes from 1.1.0 (engine 1.0 to 2.1.0).** Inputs, outputs, parameters, bundle layout and the other error codes are those of 1.x, with three differences. (1) Engine 2.1.0 weights every lesion-overlapped parcel; engine 1.0 left the lowest-numbered one out of the weighting, so the z-map of the same case and cohort differs from 1.x (see "Engine overlap behaviour" below). (2) The 1.x warning `lowest_overlapped_parcel_omitted` and the rejection `lesion_single_parcel` are gone: a lesion within one parcel now yields a map (weight 1 on that parcel). Removing a declared error code is a major version change ([versioning.md](../../docs/contracts/versioning.md#3-steps)), hence 2.0.0. (3) Engine 2.x selects controls by sex and age only with its flag `--match-age-sex` (1.0 always did; without the flag 2.x uses every participant and ignores `control_count`); the wrapper passes the flag, and a test checks both the command line and the control selection against the independent computation.

## What it computes (engine)

For one lesion: the parcels of the normative parcellation overlapped by the lesion get weights proportional to their overlap voxels; the `control_count` normative participants of the same sex with the nearest age are selected (ties: age, then subject index); each participant's lesion-weighted connectivity profile over all target parcels is formed; every target parcel is tested against zero across participants (one-sample t-test), the signed t is converted to a signed z (`sign(t) * isf(p / 2)`), and the z of each parcel is written on that parcel's voxels. Output type `fdisconnection_map`: float32 on the 2 mm MNI152NLin6Asym grid, zero outside the parcellation, NaN for a parcel whose statistic is undefined (fewer than two finite connectivity values across the controls; reported as warning `undefined_parcels`). A parcel whose connectivity is exactly constant across the controls is not undefined in 2.1.0: rounding in its regression leaves a tiny non-zero variance and the parcel gets a very large finite z (about 32.5), where 1.0 gave NaN; real connectivity is never constant, so this is recorded, not worked around. The sign is that of the lesion-to-parcel connectivity (positive: positively coupled with the lesion site), not "more disconnection": the artefact type text in `contracts/artefact-types.yaml` speaks of "larger values meaning more disconnection" and should be revisited by a contract change (see the PR).

## Contract mapping

| Contract | Engine |
|---|---|
| input `lesion_mask` (`lesion_mask_mni`) | `--lesion-dir` with `case.nii.gz` (copy in `/tmp`) |
| parameter `sex` (`female`/`male`, bound to `case.sex`) | row of `participants.tsv`, written with the label the normative table uses (the single label starting with `F`/`M`, case-insensitive) |
| parameter `age_band` (`"65-69"`, bound to `case.age_band`) | the band midpoint is the participant age; the platform never holds exact ages |
| parameter `control_count` (default 1000, the engine default) | `--match-age-sex --control-count` |
| bundle `normative` (type `normative-fc`) at `/ref/normative` | `--fc`, `--normative-subjects`, `--parcellation` |
| `case_zmap.nii.gz` | output `fdisconnection_map` |

The engine's other files (`*_controls.csv` lists the normative participants' identifiers, `*_overlap.csv`, `*_roi_z.csv`) are kept in `/tmp` and discarded; only counts reach the log. Nothing is written to `/out/extra`.

A case without a recorded sex or age band is rejected (exit 10, `participant_sex_missing` / `participant_age_missing`): the pipeline binds both parameters from the case, so deployments that enable this slot need them on the case.

## The `normative-fc` bundle

A reference bundle ([ADR-0006](../../docs/adr/0006-reference-bundles-are-data.md), [`docs/reference-data.md`](../../docs/reference-data.md)) of type `normative-fc`, mounted at `/ref/normative`:

```
bundle.yaml                                  # id, version, type: normative-fc, licence, files + hashes
fc_row_subject_col.npy                       # float, shape (n_roi, n_subject, n_roi); FC[roi, subject, roi]
normative_subjects.csv                       # columns subject_index, eid, age, sex (subject_index = FC subject axis)
standard/schaefer1000_tian54_2mm.nii.gz      # integer parcellation, labels 1..n_roi, on the 2 mm MNI152NLin6Asym grid
```

These are three of the entries of the lab's local normative data directory, under the names the engine expects (the same names and layout as for engine 1.0; the engine's stricter checks on the table are listed in [reference-data.md](../../docs/reference-data.md#the-normative-fc-bundle)). The directory's `participants.tsv`, `lesion/` and `zmaps/` are the lab's own case data and outputs and must not be in a bundle (the platform supplies the case).

**Licence constraint.** The normative connectivity data is derived from open-access databases (such as the Human Connectome Project) whose data use terms forbid redistribution. It is never downloaded by CI or agents, never committed, never baked into an image and never put in a release tarball or on-premise installer: the bundle is created and registered locally from the lab's copy, with `licence.redistributable: false` (`licence.spdx: LicenseRef-<name>` and the terms URL in `licence.terms_url`), and mounted read-only at run time. CI and the tests use only the synthetic bundle below.

## Tests and reference data

`tests/make_reference.py` writes (deterministically, nothing committed, `tests/reference/` is git-ignored) a synthetic `normative-fc` bundle (27 block parcels, 2100 participants with sex and age, about half of each, so that the engine default of 1000 controls works; integer-hash connectivity, about 6 MB), a lesion straddling 8 parcels, and the expected z-map computed with the standard library and numpy only (exact t and normal quantile functions). It is the manifest's `reference_test.generator`; the bundle is passed to the runner with `--bundle normative=steps/flnm-fast/tests/reference/bundle`, and the stroke-lnm end-to-end test registers the same bundle.

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

Rejections (exit 10, declared in `step.yaml`; the reserved codes of contract 1.1 come from the SDK): `participant_sex_missing`, `participant_age_missing`, `mask_empty`, `lesion_outside_parcellation`, `insufficient_controls`. Internal failures (exit 1): `bundle_layout_unsupported` (missing file, sex labels not recognisable, parcellation not on the working grid), `engine_failed`, `engine_output_missing`.

## Engine overlap behaviour (fixed upstream; the 1.x workaround was removed)

Engine 1.0 computed the overlap weights in `compute_roi_overlap_weights` with `roi_labels = np.flatnonzero(counts)[1:]` after the background label had already been excluded, so on every run the lowest-numbered lesion-overlapped parcel was left out of the weighting (the other weights were renormalised) and a lesion within a single parcel left nothing to weight (all-NaN map, exit 0). Engine 2.1.0 uses `roi_labels = np.flatnonzero(counts)` and weights every overlapped parcel. Checked on the code of the pinned digest and with a synthetic test (three parcels; lesions in parcels 1-3, 2-3 and 2 only): 1.0 returned parcels {2, 3}, {3} and none, 2.1.0 returns {1, 2, 3}, {2, 3} and {2}. In this step the same property is pinned by `test_lowest_overlapped_parcel_is_part_of_the_weighting` and `test_lesion_in_a_single_parcel_is_processed`, which compare the real engine with the independent computation of `tests/make_reference.py`. Step 1.x had a workaround (warning `lowest_overlapped_parcel_omitted`, rejection `lesion_single_parcel`); it was removed in 2.0.0.
