# Changelog

All versions follow semantic versioning and Conventional Commits.

## Unreleased

- STEPS-01: the step wrappers of the lab's tools live in this repository (`steps/cognet-stroke`, `steps/lnm-tracts`, `steps/ct-lesion-to-mni152`) and consume the authors' repositories unmodified at pinned commits (`upstream.env`, registry `source.upstream`, checked by `registry.py check`; `registry.py fetch` fetches them). `cognet-stroke` 1.1.0 (contract 1.2) calls the author's `compute_cdr` and `compute_overlap` (COGNET-STROKE `f98dbc8`, `nanmean`), accepts NaN in the functional map and writes `excluded_parcels`, rejects infinite values and maps without a defined value in any parcel (`disconnection_map_undefined`), reads the parcellation from the bundle, and ships a slim image (numpy, nibabel, matplotlib); a bundle builder makes the `concept-maps` bundle from the author's workflow outputs and null cache. `lnm-tracts` 1.0.1 and `ct-lesion-to-mni152` 1.0.1 are the 1.0.0 code rebuilt from here (CI compares them with the published 1.0.0 images). `stroke-lnm` 0.9.0 pins them (digests pending release, `published: false`), presets 1.7.0; `research-report` 0.4.0 is pinned to its published digest. Review fixes: `upstream.env` pins the content of the files taken from the author's repository (`UPSTREAM_SHA256`, checked in the Dockerfiles and by `steps/fetch-upstream.sh`); cognet-stroke reports a degenerate null distribution (no spread, e.g. a map without positive values) with a null p-value instead of the 1/n_nulls that made 1.0.0 call such scores significant, and warns about null maps without a defined value, tied null values and partly defined parcels (`degenerate_null_distribution`, `undefined_null_maps`, `tied_null_values`, `parcels_partly_defined`); the licence of the COGNET-STROKE code inside the image is recorded as an open question (`licence_review.status: pending`).

- COGNET-02: parcels without a value (NaN) in the functional disconnection map no longer stop a real run. `cognet-profile` schema gains the optional `excluded_parcels` `{count, fraction, reason}`; `research-report` 0.4.0 (report 1.2) accepts NaN voxels in the functional map and states in the COGNET section how many parcels were excluded, when the profile says so; `stroke-lnm` 0.8.0 pins it (digest pending release, `published: false`), presets 1.6.0.

- MVP-01: the lab `deployment` profile is the minimum pipeline (lesion mask in MNI152 -> flnm-fast and COGNET, lnm-tracts -> report); `stroke-lnm` 0.7.0 with the published step digests and bundle-pinned entries for the lab's tract atlas and concept maps; preset `mni-mask-analysis` 1.5.0 requires COGNET. Stages that cannot be used yet (normalisation, lesion segmentation, lesion bank, voxel-wise structural LNM) are listed by the API and shown greyed and labelled "In development" in the wizard and on the run page. Implementation display titles per cohort (en, es). Synthetic example inputs are shown as provided by the platform.

## 0.1.0 - Initial

- Base repository structure.
- FastAPI API with upload, listing, status and PDF download.
- Celery worker with Redis.
- CLI adapter for an external processor.
- Development dummy processor.
- Simple web GUI in Spanish.
- Docker Compose with PostgreSQL, Redis, API, worker, frontend and Nginx.
- Technical documentation for the master's thesis prototype.
