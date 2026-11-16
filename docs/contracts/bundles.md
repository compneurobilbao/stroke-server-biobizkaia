# Reference bundles 1.1 (`bundle.yaml`)

Status: normative. Contract version `1.1` (this document is unchanged since 1.0). Decision record: [ADR-0006](../adr/0006-reference-bundles-are-data.md).
Schema: [`contracts/schemas/v1/bundle.schema.json`](../../contracts/schemas/v1/bundle.schema.json). Worked example: [`contracts/examples/bundle-toy-normative-connectome.yaml`](../../contracts/examples/bundle-toy-normative-connectome.yaml).

A reference bundle is an immutable, versioned directory of data that steps read: normative connectomes, tract atlases, templates, concept maps, model weights and calibration tables. Bundles never live in Git. Consumers: bundle registry and verification (card F0-18), executors (mounting), steps (reading `bundle.yaml` for provenance), on-premise installer (redistribution), UI and report (titles, citations, synthetic flag).

## 1. Layout

A bundle is a directory whose root contains `bundle.yaml` and the files it lists. On a host it lives at `<bundle root>/<id>/<version>/` (exact root is deployment configuration, F0-18). It is mounted read-only at `/ref/<requirement name>/`.

## 2. Fields

| Field | Req. | Meaning |
|---|---|---|
| `contract_version`, `kind: Bundle` | MUST | See [versioning.md](versioning.md). |
| `id` | MUST | Kebab-case identifier, stable across versions (e.g. `hcp1065-tracts`). |
| `version` | MUST | Semantic version ([versioning.md](versioning.md#5-bundles)). |
| `type` | MUST | Kebab-case bundle type that step requirements match on (e.g. `normative-connectome`, `tract-atlas`, `concept-maps`, `segmentation-model`). The file layout of a type is defined by the steps that consume it and documented with them. |
| `title`, `description` | MUST / MAY | Human-readable. |
| `synthetic` | MAY (default `false`) | `true` for generated data (CI fixtures, demo models); the UI and report label results accordingly. |
| `licence.spdx` | MUST | SPDX expression, or `LicenseRef-<name>` for data use agreements and other non-SPDX terms. |
| `licence.redistributable` | MUST | Whether the bundle may be copied to other sites (on-premise installations). `false` for data under a data use agreement. |
| `licence.terms_url`, `licence.notes` | MAY | Where the terms are and anything an installer must know. |
| `citation` | MAY | Text to cite when results depend on the bundle; used by the report. |
| `metadata` | MAY | Flat map of scalars (or arrays of scalars) that steps or the UI read, e.g. participant counts by sex. |
| `content_digest` | MUST | Digest of the bundle content ([3](#3-content-digest)). |
| `files[]` | MUST | `{path, sha256, size_bytes}` for every file except `bundle.yaml`. Paths are relative, use `/`, and MUST NOT contain `..`. |

## 3. Content digest

`content_digest` is `sha256:` followed by the SHA-256 of the UTF-8 text built from `files[]` as follows: for each file, sorted by the bytes of `path`, the line `<sha256>  <path>\n` (two spaces, as printed by `sha256sum`); lines concatenated. It identifies the bundle content independently of `bundle.yaml` metadata such as descriptions.

The digest can be recomputed from the listed hashes alone (`contracts/validate.py` does this) and, for full verification, from the files on disk.

## 4. Verification and use

1. The bundle registry MUST verify every file's size and SHA-256, and the `content_digest`, when a bundle is registered, and MUST refuse a bundle that fails.
2. Before mounting a bundle for a step, the platform MUST at least check that `bundle.yaml` is unchanged since registration (hash) and that every listed file exists with the listed size. Full re-hashing SHOULD run periodically and MUST run after a bundle is copied to another host (e.g. the HPC filesystem).
3. A step reads `/ref/<name>/bundle.yaml` and copies `id`, `version` and `content_digest` into its `StepProvenance.bundles[]`. The orchestrator checks that they match what it mounted.
4. A bundle version is immutable: once registered, any change to files or `bundle.yaml` requires a new version.
5. Bundles whose `licence.redistributable` is `false` MUST NOT be included in release tarballs or on-premise installers; deployments that need them install them locally from the licensor.
