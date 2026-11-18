# flnm-fast

Pipeline slot `functional-disconnection`: functional lesion network z-map of one lesion, from a normative functional connectivity bundle.

flnm-fast has no source repository. Its deliverable is the public image `compneurobilbaolab/flnm-fast:1.0`, pinned here by digest (`sha256:7370e5d9ee5c67fe7e7cf1bf6ae94d4579ee0fcf07cbf99bd3e5a39af2fd8173`, labels: title `flnm-fast`, "Create lesion network z-maps", licence MIT; entry point `python /opt/lesion_zmaps/create_lesion_zmaps.py`). This step is a **wrapper**: the [`Dockerfile`](Dockerfile) starts `FROM` that digest, adds the SDK, the manifest `/opt/step/step.yaml` and the contract entry point `/usr/local/bin/run`, and changes nothing else (a few MB on top of the 625 MB base, reported by the CI job; Python, numpy, nibabel, pandas and scipy are the engine's own). The engine script is run unchanged, as a subprocess, once per run.

Version 1.1.0 is the worked example of an external tool ([steps/README.md](../README.md#wrapping-an-external-tool)): it declares contract 1.2 and the `upstream` block (engine name, version, homepage, image digest equal to the `FROM` of the `Dockerfile`, licence), and its registry entry (`pipelines/stroke-lnm/registry.yaml`) has `origin: external`, a licence review and the validation status `reference-tested`. Behaviour, engine and digest are those of 1.0.0.

## What it computes (engine)

For one lesion: the parcels of the normative parcellation overlapped by the lesion get weights proportional to their overlap voxels; the `control_count` normative participants of the same sex with the nearest age are selected (ties: age, then subject index); each participant's lesion-weighted connectivity profile over all target parcels is formed; every target parcel is tested against zero across participants (one-sample t-test), the signed t is converted to a signed z (`sign(t) * isf(p / 2)`), and the z of each parcel is written on that parcel's voxels. Output type `fdisconnection_map`: float32 on the 2 mm MNI152NLin6Asym grid, zero outside the parcellation, NaN for a parcel with zero variance across the controls (reported as warning `undefined_parcels`). The sign is that of the lesion-to-parcel connectivity (positive: positively coupled with the lesion site), not "more disconnection": the artefact type text in `contracts/artefact-types.yaml` speaks of "larger values meaning more disconnection" and should be revisited by a contract change (see the PR).

## Contract mapping

| Contract | Engine |
|---|---|
| input `lesion_mask` (`lesion_mask_mni`) | `--lesion-dir` with `case.nii.gz` (copy in `/tmp`) |
| parameter `sex` (`female`/`male`, bound to `case.sex`) | row of `participants.tsv`, written with the label the normative table uses (the single label starting with `F`/`M`, case-insensitive) |
| parameter `age_band` (`"65-69"`, bound to `case.age_band`) | the band midpoint is the participant age; the platform never holds exact ages |
| parameter `control_count` (default 1000, the engine default) | `--control-count` |
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

These are three of the entries of the lab's local normative data directory, under the names the engine expects. The directory's `participants.tsv`, `lesion/` and `zmaps/` are the lab's own case data and outputs and must not be in a bundle (the platform supplies the case).

**Licence constraint.** The normative connectivity data is derived from open-access databases (such as the Human Connectome Project) whose data use terms forbid redistribution. It is never downloaded by CI or agents, never committed, never baked into an image and never put in a release tarball or on-premise installer: the bundle is created and registered locally from the lab's copy, with `licence.redistributable: false` (`licence.spdx: LicenseRef-<name>` and the terms URL in `licence.terms_url`), and mounted read-only at run time. CI and the tests use only the synthetic bundle below.

## Tests and reference data

`tests/make_reference.py` writes (deterministically, nothing committed, `tests/reference/` is git-ignored) a synthetic `normative-fc` bundle (27 block parcels, 2100 participants with sex and age, about half of each, so that the engine default of 1000 controls works; integer-hash connectivity, about 6 MB), a lesion straddling 8 parcels, and the expected z-map computed with the standard library and numpy only (exact t and normal quantile functions). It is the manifest's `reference_test.generator`; the bundle is passed to the runner with `--bundle normative=steps/flnm-fast/tests/reference/bundle`, and the stroke-lnm end-to-end test registers the same bundle.

```sh
python -m pytest steps/flnm-fast            # wrapper tests; engine tests are skipped without the engine
# with the engine (needs pandas and scipy; extract the script from the pinned image):
docker run --rm --entrypoint cat compneurobilbaolab/flnm-fast@sha256:7370e5d9ee5c67fe7e7cf1bf6ae94d4579ee0fcf07cbf99bd3e5a39af2fd8173 \
    /opt/lesion_zmaps/create_lesion_zmaps.py > /tmp/create_lesion_zmaps.py
FLNM_FAST_SCRIPT=/tmp/create_lesion_zmaps.py python -m pytest steps/flnm-fast
python steps/flnm-fast/tests/make_reference.py
docker build -f steps/flnm-fast/Dockerfile -t flnm-fast-step:dev .
stroke-step-reftest --step-dir steps/flnm-fast --mode docker --image flnm-fast-step:dev \
    --bundle normative=steps/flnm-fast/tests/reference/bundle
```

Rejections (exit 10, declared in `step.yaml`; the reserved codes of contract 1.1 come from the SDK): `participant_sex_missing`, `participant_age_missing`, `mask_empty`, `lesion_outside_parcellation`, `lesion_single_parcel`, `insufficient_controls`. Internal failures (exit 1): `bundle_layout_unsupported` (missing file, sex labels not recognisable, parcellation not on the working grid), `engine_failed`, `engine_output_missing`.

## Known issue of the packaged engine (not changed here)

The overlap step of `create_lesion_zmaps.py` (`compute_roi_overlap_weights`) excludes background and out-of-range labels and then still drops the first element of the remaining label list (`np.flatnonzero(counts)[1:]`, meant to skip the background label). The effect, on every run: the lowest-numbered lesion-overlapped parcel is left out of the lesion weighting and the other weights are renormalised; a lesion confined to a single parcel leaves nothing to weight, and the engine then ends successfully with an all-NaN map. The wrapper does not alter the engine; it records warning `lowest_overlapped_parcel_omitted` in every run, rejects the single-parcel case as `lesion_single_parcel`, and the reference expectations reproduce the engine's behaviour (a test pins it: `test_engine_omits_the_lowest_overlapped_parcel`). When the base image is rebuilt without this behaviour, bump the digest and the step version, set `ENGINE_DROPS_LOWEST_OVERLAPPED_PARCEL` to `False` in `step.py` and in `tests/make_reference.py`, and drop the warning and the rejection.
