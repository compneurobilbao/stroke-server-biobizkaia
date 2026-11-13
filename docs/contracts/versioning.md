# Versioning and compatibility

Status: normative. Contract version `1.0`.

Five things carry versions: the contract itself (all schemas together), steps, pipelines, bundles and the artefact type registry. Every version except the contract version is a [Semantic Versioning 2.0](https://semver.org/) string `MAJOR.MINOR.PATCH` with an optional pre-release suffix and no build metadata.

## 1. What is breaking

A change is **breaking** for a document type if a consumer written for the old version can fail, or silently compute something different, when it meets the new one. In particular:

- removing or renaming a field, port, parameter, slot, input, error code, artefact type or grid;
- making an optional field required, or adding a required field;
- narrowing a type, enum, pattern or range;
- changing the meaning or units of an existing value.

Non-breaking (**additive**) changes add optional fields, enum values or types, widen patterns and ranges, or clarify text without changing meaning.

## 2. The contract version

1. `contract_version` is `"MAJOR.MINOR"` and is the same for all six schemas (`step`, `pipeline`, `bundle`, `provenance`, `error`, `artefact-types`). Every document carries it. This specification is `1.0`.
2. Schemas live in `contracts/schemas/v<MAJOR>/`. A minor release edits them in place with additive changes only; a major release creates a new directory.
3. A minor contract change MUST only relax the schemas, so that every document valid under `1.N` is valid under `1.M` for M > N.
4. **Compatibility rule.** A component that implements contract `X.Y` (orchestrator, SDK, validator) MUST accept a document whose `contract_version` is `X.Z` with Z ≤ Y, and MUST reject, with a message naming both versions, a document with another major version or with Z > Y. (`contracts/validate.py` declares the version it implements in `CONTRACT_MAJOR`/`CONTRACT_MINOR`.)
5. A step's `StepProvenance` and `StepError` carry the same `contract_version` as its manifest.
6. A contract change after F0-10 requires a new ADR (plan section 7). A major change requires a migration note listing the steps and pipelines to update; the platform SHOULD support the previous major version for at least one platform release.

*Rationale for one version for all schemas:* the documents reference each other (a pipeline embeds step identities, provenance embeds errors and bundle records); a single number makes "which contract does this step implement" one answer.

## 3. Steps

A step `name` + `version` identifies exactly one image digest; rebuilding the image requires a new version.

| Bump | When |
|---|---|
| MAJOR | Any breaking interface change: an input or output port removed, renamed or retyped; a required input added; an output changed from required to optional; a parameter removed, renamed or narrowed; `provides` changed; an error code removed; a bundle requirement made incompatible with previously accepted bundles; the contract major version changed. |
| MINOR | Additive interface changes (new optional input, new output, new parameter whose default preserves previous behaviour, new error code), and **any change that alters outputs on the reference dataset** beyond the frozen tolerances, including dependency upgrades and loosened tolerances. |
| PATCH | Anything else. A patch release MUST pass the previous version's reference test unchanged. |

Pipelines pin exact step versions and digests, so a new step version reaches users only through a new pipeline version or a deployment change that enables it.

## 4. Pipelines

| Bump | When |
|---|---|
| MAJOR | Run inputs or input sets removed or changed; a non-optional slot added or removed; slot ports or wiring changed incompatibly; an artefact type of a slot port changed. |
| MINOR | Optional slot added; implementation added, removed or replaced (new step version or digest); default implementation changed; parameter overrides, bindings or bundle pins changed. |
| PATCH | Titles and descriptions only. |

A run records the pipeline `id`, `version` and the SHA-256 of the exact file. Deployment configuration (what is enabled) is not versioned with the pipeline and is recorded per run through the slot records.

## 5. Bundles

| Bump | When |
|---|---|
| MAJOR | The file layout or format changes so that a step written for the previous version cannot read it. |
| MINOR | Content changes in the same layout (more participants, re-processed data, retrained weights). |
| PATCH | `bundle.yaml` metadata only (description, citation, notes); file hashes identical. |

Step requirements SHOULD use ranges that accept minor updates (`^1.0.0`). Results still change with minor bundle updates; provenance records the exact version and digest.

## 6. Artefact type registry

| Bump | When |
|---|---|
| MAJOR | A type or grid removed or renamed, or its format, extension, constraints, semantics or BIDS naming changed. |
| MINOR | A type or grid added; a `json_schema` or `tsv_columns` added to a type that had none, if every existing producer already complies. |
| PATCH | Descriptions only. |

## 7. Version ranges

Bundle requirements use a comma-separated list of comparators, all of which must hold: `==`, `>=`, `>`, `<=`, `<`, and `^` (same major, greater or equal). Example: `">=1.2.0, <2.0.0"`. A pre-release version satisfies a range only if it compares within it under SemVer precedence (so `1.0.0-rc.1` does not satisfy `>=1.0.0`).

## 8. Checks performed by the orchestrator

When loading a pipeline and when registering or enabling an implementation, the orchestrator MUST, in this order:

1. check `contract_version` of every document (rule 2.4);
2. validate every document against its schema;
3. run the single-document rules of the specification (parameter defaults, wiring, acyclicity, static availability, bundle digest formula);
4. check every artefact type against the loaded registry;
5. check every implementation against its manifest (matching rules M1–M5 in [pipeline.md](pipeline.md#41-matching-a-step-to-a-slot)), its literal parameters and bindings against the parameter schema, and its bundle pins against the requirement ranges and the registered bundles' types;
6. refuse anything that fails, with the validator's message.

`contracts/validate.py` implements checks 1–5 for files given on one command line, and is the reference for the orchestrator's implementation.
