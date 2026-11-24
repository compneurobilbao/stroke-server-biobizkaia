# Changelog

All versions follow semantic versioning and Conventional Commits.

## Unreleased

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
