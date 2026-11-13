# Artefact types 1.0

Status: normative. Contract version `1.0`. Registry: [`contracts/artefact-types.yaml`](../../contracts/artefact-types.yaml); schema: [`contracts/schemas/v1/artefact-types.schema.json`](../../contracts/schemas/v1/artefact-types.schema.json).

Artefact types are the vocabulary that makes step inputs, step outputs, pipeline inputs and slot ports comparable. Every port in a manifest or pipeline names one type. Consumers: validator and orchestrator (matching and wiring), SDK and orchestrator (format checks on input and output), case store (file naming, card F0-17), viewer and report (what a file is).

## 1. The registry

The registry is one YAML file versioned in this repository, with its own semantic version ([versioning.md](versioning.md#6-artefact-type-registry)). It defines:

- `grids`: named voxel grids `{space, shape, affine}`;
- `types`: one entry per type, keyed by a snake_case name.

The platform loads exactly one registry; its version is recorded in `RunProvenance.platform.artefact_types_version`.

## 2. Type definition

| Field | Req. | Meaning |
|---|---|---|
| `description` | MUST | What the file contains, including units and value semantics where they matter for interchangeability. |
| `format` | MUST | `nifti`, `json`, `tsv`, `pdf` or `png`. |
| `extension` | MUST | `.nii.gz`, `.json`, `.tsv`, `.pdf` or `.png`, consistent with `format`. Used for `/in/<port><ext>` and `/out/<port><ext>`. |
| `bids` | MUST | `{datatype, suffix, entities}` used by the case store to name the file. Entities are limited to `space`, `res`, `label`, `desc` and `stat`. |
| `nifti` | MAY (nifti only) | Constraints: `binary` (values in {0, 1}), `space`, `grid` (a key of `grids`; shape and affine must match within 1e-4 mm). |
| `json_schema` | MAY (json only) | Path, relative to `contracts/`, of a JSON Schema the file must satisfy. |
| `tsv_columns` | MAY (tsv only) | Columns that MUST be present (header row, tab-separated, UTF-8); more columns are allowed. |

## 3. Format checks

The SDK MUST check every staged input against its type before the step starts work, and every output before the step exits 0. The orchestrator MUST check outputs when collecting; a failure is a `contract_violation`. Checks per format:

- `nifti`: a readable gzip-compressed NIfTI-1 or NIfTI-2 file; if `binary`, all values are 0 or 1; if `grid`, shape and affine match the grid.
- `json`: valid UTF-8 JSON; if `json_schema`, valid against it.
- `tsv`: UTF-8, tab-separated, header row containing `tsv_columns`.
- `pdf`, `png`: the file starts with the format's signature.

The orchestrator's checks are deliberately cheap (headers, not voxel statistics), except `binary`, which it MAY skip.

## 4. Naming in the case store

Steps never name files after subjects. The case store builds the stored name from the case's BIDS entities and the type:

```
sub-<subject>_ses-<session>[_space-<v>][_res-<v>][_label-<v>][_desc-<v>][_stat-<v>]_<suffix><extension>
```

with entities in that order (the BIDS order), under `derivatives/<step name>-<step version>/sub-<subject>/ses-<session>/<datatype>/` for derivatives (D5). When two output ports of one step have the same type, the case store adds `desc-<port name without underscores>` to keep names unique. The exact tree is card F0-17's responsibility; this section fixes only the inputs to it.

## 5. Types in registry 1.0.0

| Type | Format | Notes |
|---|---|---|
| `ct_native` | nifti | Head CT, native space, HU. |
| `lesion_mask_native` | nifti, binary | On the grid of the accompanying CT. |
| `lesion_mask_mni_any` | nifti, binary, MNI152NLin6Asym | User-supplied MNI mask on any grid; only intake consumes it. |
| `lesion_mask_mni` | nifti, binary, grid `MNI152NLin6Asym-res2` | The pipeline's working-grid lesion mask. |
| `ct_mni` | nifti, grid `MNI152NLin6Asym-res1` | Registered CT for QC. |
| `qc_figure` | png | QC figure of any step. |
| `lesion_qc` | json | Intake checks; schema to be added by card F0-22. |
| `fdisconnection_map` | nifti, grid `MNI152NLin6Asym-res2` | Functional disconnection; statistic fixed by card F0-24. |
| `tract_disconnection` | tsv `tract`, `disconnection` | Fraction of each tract's streamlines intersecting the lesion. |
| `cognet_profile` | json | Schema to be added by card F0-26. |
| `report_pdf`, `report_json` | pdf, json | Report and its canonical content. |
| `run_provenance` | json, provenance schema | Produced only by the pseudo-source `run.provenance`. |

### 5.1 Working grid

All MNI-space voxel maps exchanged between steps use one grid, `MNI152NLin6Asym-res2`: the 2 mm MNI152 grid distributed with FSL (91 × 109 × 91 voxels). Intake steps resample user-supplied MNI masks to it; normalisation writes its mask on it; steps that need another grid resample internally and write their outputs back on it.

*Rationale:* one grid makes every MNI artefact directly comparable and overlayable in the viewer, removes a class of silent errors, and matches the example in D1 (`space-MNI152NLin6Asym_res-2_label-lesion_mask.nii.gz`). The choice of 2 mm rather than 1 mm is flagged for the project owner and for the method cards (F0-23, F0-24, F0-25): changing it later is a major registry change.
