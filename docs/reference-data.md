# Reference data: the bundle registry

Status: implemented by card F0-18. Contract: [docs/contracts/bundles.md](contracts/bundles.md); decision: [ADR-0006](adr/0006-reference-bundles-are-data.md). Code: `orchestrator/bundles.py` (library and CLI), `scripts/bundles/registry.py` (CLI entry point), table `reference_bundles` ([data-model.md](data-model.md) 4.9).

Reference data (normative connectomes, tract atlases, templates, model weights) is never in Git. It is stored as immutable, versioned bundles under one **bundle root** per host, registered and verified by the registry, and mounted read-only at `/ref/<requirement name>/` in the step container.

## Layout on a host

```
<bundle root>/<bundle id>/<version>/bundle.yaml
                                    <files listed in bundle.yaml>
<bundle root>/.registry.json        # only with the default JSON store
```

The bundle root is deployment configuration (`--root` or the `BUNDLE_ROOT` environment variable). Mount the root read-only into the API, worker and executors where possible; only the registration command needs write access.

## Preparing a bundle

1. Put the files in a directory and write `bundle.yaml` with `id`, `version`, `type`, `title`, `licence.spdx`, `licence.redistributable` (both required), optional `citation`, `metadata`, `synthetic`, and the `files` list with SHA-256 and size of every file. Compute `content_digest` as specified in bundles.md section 3 (`contracts.validate.bundle_digest`).
2. Check the manifest: `python contracts/validate.py path/to/bundle.yaml`.
3. Register it (below). The registry re-hashes everything itself; a manifest that disagrees with the files is refused.

## Command line

```bash
export BUNDLE_ROOT=/srv/bundles          # or pass --root
python scripts/bundles/registry.py register path/to/bundle-dir
python scripts/bundles/registry.py list [--type normative-connectome] [--status active] [--json]
python scripts/bundles/registry.py verify                      # full re-hash of all bundles
python scripts/bundles/registry.py verify <id> <version> [--quick]
python scripts/bundles/registry.py withdraw <id> <version>
python scripts/bundles/registry.py resolve steps/<step>/step.yaml   # what would be mounted
```

`python -m orchestrator.bundles` is the same command. `--store db` writes the `reference_bundles` table (needs the backend environment, `DATABASE_URL`) instead of `<root>/.registry.json`. Exit status is 1 on any error or failed verification.

What each command guarantees:

- `register` verifies the manifest against the schema (licence required), the `content_digest` formula, and every file's size and SHA-256; it also refuses files on disk that the manifest does not list. It copies the bundle into the root (unless already there) and verifies the copy. Registering the same id and version with different content is refused: versions are immutable. Registering identical content again is a no-op.
- `verify` records `verified` or `failed`. A failed bundle is never resolved until a later full verification passes (for example after re-copying it from the source). Run it after copying a bundle to another host (HPC) and periodically.
- `withdraw` stops a bundle from being used by new runs; past runs keep their reference.

## For executors and the orchestrator

```python
from orchestrator.bundles import BundleRegistry, BundleError, mount_specs

registry = BundleRegistry(bundle_root)            # JSON store; SqlAlchemyStore(session) for the DB
resolved = registry.resolve(step_manifest)        # {name: ResolvedBundle}
for host, container, mode in mount_specs(resolved):   # (host path, "/ref/<name>", "ro")
    ...
```

`step_manifest` is the parsed `step.yaml`; its `bundles` requirements (`name`, `type`, optional `id`, `version` range, `required`) are matched against active, verified bundles, taking the highest matching version unless `selection={name: (id, version)}` pins one (pipelines or deployments may pin: an implementation entry of a pipeline can carry `bundles: {name: {id, version}}`, which is how `stroke-lnm` offers one cohort per implementation). When bundles of several ids satisfy a requirement that has no `id` and no pin, the choice is ambiguous: it is still deterministic (highest version; equal versions are broken by smallest bundle id, then earliest registration) and a warning is logged, so give the requirement an `id` or pin the bundle. Before returning, each chosen bundle gets the cheap pre-mount check of bundles.md section 4.2 (manifest hash unchanged since registration, every listed file present with the listed size); `full=True` re-hashes, and must be used after a bundle was copied to another host.

The step is refused, with an exception that names every problem, when a required bundle is missing, withdrawn, out of range or fails verification: `BundleNotFoundError` or `BundleVerificationError` (both `BundleError`). The caller must not start the step container then. `ResolvedBundle` carries `bundle_id`, `version` and `content_digest`, which the orchestrator compares with what the step reports in its provenance.

## Limits and open points

- The cheap check does not detect a same-size content change; that needs a full verification (scheduled or forced with `full=True`).
- Bundle layouts per bundle type are defined by the consuming steps. The `tract-atlas` type has the layout the structural-disconnection step reads (`labels.txt` plus one `<tract>.tck` per tract at the bundle root); the synthetic fixture `toy-tract-atlas` uses it. The `normative-fc`, `tract-atlas`, `space-transform`, `concept-maps` and `slnm-coarse-cache` types are described below. The lab's deployment profile pins `hcp1065-tracts` for `tract-atlas` (entry `lnm-tracts-hcp1065`; 1.0.0 since `stroke-lnm` 0.7.0, 1.1.0 with `xfm-nlin6asym-to-nlin2009aasym` 1.0.0 for `space-transform` since 0.10.0) and `cognet-concept-maps` 1.0.0 for `concept-maps` (entry `cognet-stroke-concept-maps`) since 0.7.0; CI keeps the unpinned entries and its synthetic bundles.
- Installers and release tarballs must skip bundles whose `licence.redistributable` is `false`.

## The `normative-fc` bundle

Bundle type read by the functional disconnection step `flnm-fast` ([`steps/flnm-fast`](../steps/flnm-fast/README.md), mounted at `/ref/normative`): the normative parcel-by-parcel functional connectivity of a healthy cohort. One bundle is one cohort, and the step uses every participant of it: there is no age or sex matching, so the bundle needs no age or sex labels. The cohort is chosen by the pipeline, which pins a bundle id and version per implementation entry (`stroke-lnm` 0.6.0: `normative-fc-hcp-aging` 1.0.0 as `flnm-fast-hcp-aging`, the default, and `normative-fc-hcp-ya` 1.0.0 as `flnm-fast-hcp-ya`; the synthetic `synthetic-normative-fc` 1.0.0 for CI only).

```
bundle.yaml                          # id, version, type: normative-fc, licence, files; metadata.parcellation (optional)
fc_row_subject_col.npy               # float array, shape (n_roi, n_subject, n_roi): FC[roi, subject, roi]
normative_subjects.csv               # required column subject_index (position on the subject axis); one row per participant
standard/<parcellation>.nii.gz       # integer parcellation, labels 1..n_roi, on the 2 mm MNI152NLin6Asym grid (91 x 109 x 91)
```

Required files: the array, the table and one parcellation. The table needs `subject_index` only: it must have exactly one row for every position 0 to n_subject-1 of the array (no gaps, no duplicates), and any other columns (`subject_id`, `source_file`, site, framewise displacement, even `age` and `sex`) are ignored. The real cohorts have `subject_id` and no age or sex (HCP Young Adult: 2566 x 1000 x 2566; HCP Aging: 2566 x 627 x 2566, with source file, site and FD columns). Optional metadata: `metadata.parcellation` names the parcellation file relative to the bundle root (for example `standard/firsT_2566.nii.gz`); without it the bundle must contain exactly one `*.nii.gz` directly under `standard/`, which is used. Several candidates and no metadata, or a path that is not a `.nii.gz` file inside the bundle, make the step fail with `bundle_layout_unsupported`. The file names `fc_row_subject_col.npy` and `normative_subjects.csv` are those the engine reads by default; the lab's local normative data directories also hold `participants.tsv`, `lesion/` and `zmaps/` (its own cases and outputs): they are **not** part of the bundle and must not be copied into it.

**Licence.** The normative data comes from open-access databases (such as the Human Connectome Project) whose data use terms forbid redistribution. A real `normative-fc` bundle therefore has `licence.redistributable: false` and `licence.spdx: LicenseRef-<name>` (terms in `licence.terms_url`, citation in `citation`); it is created and registered on each host from the lab's own copy (`scripts/bundles/registry.py register`), is never committed, never baked into an image, never part of a release tarball or on-premise installer, and is mounted read-only at run time. Agents and CI never download it. CI and the tests use the synthetic bundle `synthetic-normative-fc` (27 block parcels, 200 participants, `synthetic: true`, CC0-1.0), which `steps/flnm-fast/tests/make_reference.py` writes deterministically.

Preparing a real bundle: copy the three items above into an empty directory, write `bundle.yaml` as in [Preparing a bundle](#preparing-a-bundle) (`type: normative-fc`, `licence.redistributable: false`, `metadata.parcellation` set to the parcellation path, participant counts if wanted), then `register` it. The bundle is large (a normative FC array is several gigabytes): keep it on local storage, mount the bundle root read-only, and copy it to the HPC filesystem with a full `verify` afterwards.

## The `tract-atlas` bundle

Bundle type read by the tract-wise disconnection step `lnm-tracts` ([`steps/lnm-tracts`](../steps/lnm-tracts/README.md), mounted at `/ref/tract_atlas`): a tract atlas as streamline files in the millimetre coordinates of the space named by `metadata.space` (MNI152NLin6Asym, the lesion space, when absent).

```
bundle.yaml          # id, version, type: tract-atlas, licence, files; metadata.space (optional)
labels.txt           # one tract name per line ([A-Za-z0-9_.+-], at most 64 characters, unique); row order is the table order
<tract name>.tck     # one MRtrix .tck file per name, at the bundle root, at least one streamline each
```

This is the layout of the former `yeh_tracts` directory of the lnm repository, so the HCP1065 atlas needs no conversion of its files. CI uses the synthetic `toy-tract-atlas` fixture (`fixtures/data/bundles`) and the step's own toy atlases (`steps/lnm-tracts/tests/make_reference.py`, one of them declaring a synthetic space).

**Atlas space.** Since `lnm-tracts` 1.1.0 an atlas whose `metadata.space` is not `MNI152NLin6Asym` is used only together with a `space-transform` bundle for the pair MNI152NLin6Asym to that space (below): the lesion is resampled into the atlas space by nearest neighbour before the streamlines are tested. Without `metadata.space` the step behaves as 1.0.x. The HCP1065 atlas is in ICBM 2009a nonlinear asymmetric (`MNI152NLin2009aAsym`), which `hcp1065-tracts` 1.0.0 did not declare, so 1.0.x tested its streamlines against a lesion in another template space. The lab's deployment therefore pins `hcp1065-tracts` 1.1.0 since `stroke-lnm` 0.10.0: the same `labels.txt` and `.tck` files as 1.0.0 (same `content_digest`) with `metadata.space: MNI152NLin2009aAsym` and the new version in `bundle.yaml`. Bundle versions are immutable, so 1.1.0 is a new directory: copy the files of 1.0.0, edit `version` and `metadata` in the copy of `bundle.yaml`, then `register` it (1.0.0 stays registered for the runs that used it).

## The `space-transform` bundle

Bundle type read by `lnm-tracts` 1.1.0 and later (mounted at `/ref/space_transform`, optional requirement `space_transform`): a spatial transform between two template spaces as a coordinate map.

```
bundle.yaml              # id, version, type: space-transform, licence, files; metadata.from_space, metadata.to_space
coordinate_map.nii.gz    # float32, shape (X, Y, Z, 3), on a grid of to_space: voxel (i, j, k) holds the RAS mm position in from_space of its centre
```

The map is the image-resampling direction (ANTs and TemplateFlow `mode-image`): an image of `from_space` is brought to `to_space` by looking it up at the stored positions, which needs only nibabel and numpy in the step. Other files (`LICENSE`, `README.md`, `verification.json`) are allowed and listed in `files`.

**`xfm-nlin6asym-to-nlin2009aasym` 1.0.0** (MNI152NLin6Asym to MNI152NLin2009aAsym, for the HCP1065 atlas) is built by [`scripts/bundles/space_transform/build.sh WORK_DIR`](../scripts/bundles/space_transform/build.sh) on a host with ANTs (`antsApplyTransforms` on PATH, or `ANTS_IMAGE` naming a Docker image that contains it) and a Python with nibabel, numpy and PyYAML. It downloads every source from TemplateFlow at a pinned URL and SHA-256 (the transform `tpl-MNI152NLin2009cAsym_from-MNI152NLin6Asym_mode-image_xfm.h5`, the 2009a 1 mm T1w template and brain mask that define the target grid, the NLin6Asym 1 mm T1w template and the template `LICENSE`), composes the transform into a displacement field on the 2009a grid with `antsApplyTransforms`, and converts it from ITK (LPS) to a RAS coordinate map (`make_bundle.py`). The build fails unless the map reproduces the ANTs resampling of the NLin6Asym template (correlation at least 0.9999 in the brain) and improves its correlation with the 2009a template; the measurements and displacement statistics are kept in `verification.json`. About 100 MB; nothing is committed, and the bundle is registered like any other. `tests/test_space_transform_bundle.py` checks the conversion on synthetic inputs.

Open points of this bundle (recorded, not resolved):

- **Source transform.** TemplateFlow publishes a transform from MNI152NLin6Asym to MNI152NLin2009cAsym, not to 2009a. The MNI page of the ICBM 2009 templates describes 2009a and 2009c as the same anatomy with a different sampling and N3 version, so the 2009c transform is applied on the 2009a grid. The build does not measure the 2009a/2009c equivalence itself.
- **Licence of the transform.** The bundle carries the MNI ICBM 2009 template licence (permissive; the copyright notice must accompany every copy) copied from the TemplateFlow `tpl-MNI152NLin2009cAsym` archive, whose description refers to that file. TemplateFlow states no separate licence, author or method for the transform file itself; `licence.redistributable: true` follows the archive's licence and should be confirmed before the bundle is redistributed outside the lab.
- **Not built yet.** Version 1.0.0 has not been built or registered on a deployment; `verification.json` of the first build is the first measurement of the conversion on the real templates.

## The `concept-maps` bundle

Bundle type read by the COGNET-STROKE step `cognet-stroke` ([`steps/cognet-stroke`](../steps/cognet-stroke/README.md), mounted at `/ref/concept_maps`): the concept masks and their null masks over the parcels of a parcellation.

```
bundle.yaml              # id, version, type: concept-maps, licence, files; metadata (n_parcels, n_nulls, parcellation, methods)
parcellation.nii.gz      # integer labels (an integer-valued float array is accepted) on the 2 mm MNI152NLin6Asym grid, 0 = background
concepts.tsv             # columns concept and domain (snake_case); row order is the profile order
concepts/<concept>.npz   # mask: (n_parcels,) in {1, -1} (1 inside the concept); nulls: (n_nulls, n_parcels) in {1, -1}
```

The parcels are the sorted non-zero labels of `parcellation.nii.gz`, and every vector follows that order; the step reads the parcel count from the parcellation and fixes none, so a bundle built on another parcellation needs no step change. All concepts have the same number of null masks.

**Building it from the author's workflow.** `steps/cognet-stroke/bundle/build_bundle.py` turns the outputs of the COGNET-STROKE workflow (`src/generate_cdr_report/cdr_report_workflow.py`) into this layout without computing anything statistical itself: the parcellation is the author's (default `firsT_2566.nii.gz` of the checkout, 2566 parcels, copied byte for byte); a concept's `mask` is its FDR-corrected z map (`<concept>_z_desc-association_level-voxel_corr-FDR_method-indep.nii.gz`) reduced to parcels with neuromaps' `Parcellater` and thresholded at z > 2, as the workflow does; `nulls` is the workflow's null cache `<concept>_nulls_thr_<parcellation name>.npy` (brainsmash surrogates with the workflow's parameters, thresholded the same way); `concepts.tsv` defaults to the author's 22 concepts with the provisional domain grouping of `cognet-concept-maps` 1.0.0 (`steps/cognet-stroke/bundle/concepts.tsv`; the workflow defines no domains). Run it in the author's analysis environment (for neuromaps), on the host that holds the workflow outputs:

```bash
python steps/cognet-stroke/bundle/build_bundle.py --upstream /path/to/COGNET-STROKE \
    --maps-dir /path/to/concept_data_dir/corrected_fdr --version 1.1.0 --out /path/to/cognet-concept-maps-1.1.0
python contracts/validate.py /path/to/cognet-concept-maps-1.1.0/bundle.yaml
python scripts/bundles/registry.py register /path/to/cognet-concept-maps-1.1.0
```

A bundle regenerated with another parcellation or other null parameters in the same layout is a minor version (`versioning.md` section 5), so the step's requirement (`>=1.0.0, <2.0.0`) accepts it; the pipeline entry `cognet-stroke-concept-maps` pins `cognet-concept-maps` 1.0.0 until a pipeline version pins the new one. The concept maps derive from Neurosynth data (ODbL-1.0); the builder defaults to `licence.redistributable: false`, so the bundle stays on the host until the owner decides otherwise. No concept-map data is committed or used in CI: CI uses the synthetic `synthetic-concept-maps` bundle written by `steps/cognet-stroke/tests/make_reference.py`.

## The `slnm-coarse-cache` bundle

Bundle type read by the parcel-level structural disconnection step `slnm-coarse` ([`steps/slnm-coarse`](../steps/slnm-coarse/README.md), mounted at `/ref/cohort_cache`; the step is in development and not yet in a pipeline): the portable per-participant cache of the author's `slnm-coarse` command (fast-slnm) and the participant table of the normative cohort. No tractogram is needed at lesion time.

```
bundle.yaml                       # id, version, type: slnm-coarse-cache, licence, files; metadata.layout: slnm-coarse-cache-v1
participants.csv                  # subject_label (without _DiffusionRecommended), site (MGH, UCLA, UMinn, WashU)
cache/firsT_2566_MWMA400_min20.nii.gz          # common atlas on the 2 mm working grid, labels up to 2966 (gaps kept)
cache/<subject_label>_DiffusionRecommended/    # metadata.json, atlas_dwispace.nii.gz, index/{roi_offsets,track_ids,roi_weight_totals}.npy,
                                               # dwi_bzero.nii.gz, dwireg/standard2dwi0GenericAffine.mat, dwireg/standard2dwi1Warp.nii.gz,
                                               # streamlines/sift2_weights.txt
```

**Licence.** The cache is derived from the HCP Aging diffusion data (native atlases, streamline indexes, SIFT2 weights, registrations), whose data use terms forbid redistribution: `licence.redistributable: false`, `licence.spdx: LicenseRef-<name>`, built and registered on the server only, never committed, never in an image, a release tarball, an installer or CI. CI and the tests use the synthetic `synthetic-slnm-coarse-cache` (nine participants, eight block parcels, identity transforms, `synthetic: true`, CC0-1.0) that `steps/slnm-coarse/tests/make_reference.py` writes.

**Building it on the server from the author's `coarse_files` cache.** The author builds the cache once with `slnm-prepare --copy-inputs` (or converts a legacy cache with `slnm-materialize`, on the machine with the original inputs, and checks a downloaded copy with `slnm-materialize --verify-downloaded`); see the author's `docs/preparation.md`. A cache whose subjects are symbolic links to the original inputs (`inputs_mode: symlink`) is not portable and is refused. Then, on the server, in any Python environment of this repository:

```bash
python steps/slnm-coarse/bundle/build_bundle.py \
    --coarse-files /path/to/coarse_files \
    --participants /path/to/participants.csv \
    --dest /path/to/work/slnm-coarse-cache-hcp-aging \
    --id slnm-coarse-cache-hcp-aging --version 1.0.0 \
    --licence-spdx LicenseRef-<cohort terms> --terms-url <terms URL>
python contracts/validate.py /path/to/work/slnm-coarse-cache-hcp-aging/bundle.yaml
python scripts/bundles/registry.py register /path/to/work/slnm-coarse-cache-hcp-aging
```

The builder copies, for the participants of the table only (the normative subset the author uses; extra columns are dropped), exactly the files `slnm-coarse` reads, plus the atlas, and never a `.tck`, an inverse warp or an intermediate file (`--hardlink` links instead of copying on the same file system). It refuses a cache that `slnm-coarse` would refuse: a missing file, a symbolic link or `inputs_mode` other than `copy`, a `metadata.json` that names another participant or another atlas (SHA-256 fingerprint), an atlas off the 2 mm MNI152NLin6Asym working grid or without label 2966, a table without the four sites or with at most four participants, or a label with the `_DiffusionRecommended` suffix. It writes `bundle.yaml` with every file's SHA-256 and the content digest. It computes nothing scientific and changes no copied file. Mount the registered bundle read-only; copy it to another host with a full `verify` afterwards.

Open points of this bundle (recorded, not resolved):

- **Not built yet.** The cohort cache arrives later; the number of participants, the bundle size and the build time are not known.
- **Time per lesion unknown.** The step's `resources` (2 CPUs, 8 GB, 240 minutes) and the default of 2 workers are provisional until a lesion has been run on the real cache.
- **Atlas grid.** The author documents the atlas as the 2 mm MNI grid (91 x 109 x 91); the builder and the step check that it is exactly the platform's working grid. If the real atlas differs in its affine, the step would need a resampling decision (a new step version), not a silent workaround.
