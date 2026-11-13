# Step contract specification

Normative specification of contract version **1.0**: how processing tools (steps) are packaged and run, how pipelines are declared, and what provenance the platform keeps. Plan references: decisions D1–D8 and D15, card F0-10. Decision records: [docs/adr/](../adr/README.md).

| Document | Defines | Schema |
|---|---|---|
| [step-contract.md](step-contract.md) | `step.yaml`, invocation, filesystem, environment, behaviour, logging, exit codes, `error.json`, Docker and Apptainer constraints | [step](../../contracts/schemas/v1/step.schema.json), [error](../../contracts/schemas/v1/error.schema.json) |
| [pipeline.md](pipeline.md) | `pipeline.yaml`: inputs, input sets, slots, wiring, conditions, implementations, run resolution | [pipeline](../../contracts/schemas/v1/pipeline.schema.json) |
| [provenance.md](provenance.md) | `provenance.json`: step and run records, accounting | [provenance](../../contracts/schemas/v1/provenance.schema.json) |
| [bundles.md](bundles.md) | `bundle.yaml`, content digest, verification, licences | [bundle](../../contracts/schemas/v1/bundle.schema.json) |
| [artefact-types.md](artefact-types.md) | The artefact type registry, formats, grids, naming inputs for the case store | [artefact-types](../../contracts/schemas/v1/artefact-types.schema.json) |
| [versioning.md](versioning.md) | Versions of the contract, steps, pipelines, bundles and registry; compatibility checks | — |

Machine-readable parts live in [`contracts/`](../../contracts/): JSON Schemas (draft 2020-12) in `schemas/v1/`, the artefact type registry `artefact-types.yaml`, worked examples in `examples/` (with schema-invalid counter-examples in `examples/invalid/`), and the validator.

## Validating documents

```sh
python contracts/validate.py contracts/examples/*.yaml contracts/examples/*.json
python contracts/validate.py --kind step path/to/step.yaml
python contracts/validate.py --strict pipelines/stroke-lnm/pipeline.yaml steps/*/step.yaml
```

The kind is detected from the `kind` field (or the file name). Files given together are cross-checked: pipeline implementations against the step manifests supplied, bundle pins against bundles, provenance against manifests and pipeline. Implementations whose manifest is not supplied are reported as notes, or as errors with `--strict`. Exit status is 0 when everything is valid. Tests: `tests/test_contracts.py`.

## Conventions

- The key words MUST, MUST NOT, REQUIRED, SHALL, SHALL NOT, SHOULD, SHOULD NOT, RECOMMENDED, MAY and OPTIONAL are to be interpreted as described in RFC 2119 and RFC 8174 when, and only when, they appear in capitals.
- Identifiers: step names, slot names, pipeline ids, bundle ids and bundle types are kebab-case (`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`, at most 64 characters); port names, parameter names, artefact type names, error codes and bundle requirement names are snake_case (`^[a-z][a-z0-9]*(_[a-z0-9]+)*$`).
- File hashes are lowercase hexadecimal SHA-256 in fields named `sha256`; image and bundle digests are `sha256:<hex>` strings.
- Image references are fully qualified (`registry/namespace/name`); pipelines pin them by digest. This avoids registry defaults that differ between Docker and Apptainer.
- Timestamps are RFC 3339, UTC, with a literal `Z`.
- YAML documents MUST map to JSON and MUST parse identically under YAML 1.1 and 1.2 loaders: quote version-like strings (`"1.0"`) and words such as `yes`, `no`, `on`, `off`.
- Every schema rejects unknown fields. Every field has a named consumer in the tables of the documents above.
- Text in documents, messages and logs is English. Nothing in any contract document identifies a person, a host or an institution.

## Decisions taken in this specification beyond the plan

The plan fixes the decisions D1–D8 and D15. Where it is silent, this specification decides; each item below states where the rationale is. The project owner may veto any of them; a veto before the first step is built costs only a text change.

1. One contract version for all schemas; strict schemas; newer minor versions are rejected, older accepted ([versioning.md §2](versioning.md#2-the-contract-version)).
2. The manifest is baked into the image at `/opt/step/step.yaml` and read from the image by digest; `step.yaml` names only the image repository, pipelines pin digests ([step-contract.md §1.1](step-contract.md#11-location)).
3. Fixed command `/usr/local/bin/run`; image entry points are ignored ([§2.1](step-contract.md#21-command)).
4. Files are staged as `/in/<port><ext>` and written as `/out/<port><ext>`; containers never see subject identifiers; one file per port; `/out/extra/` for diagnostics ([§2.2](step-contract.md#22-filesystem)).
5. A shared artefact type registry with named voxel grids, and a single working grid, MNI152NLin6Asym 2 mm ([artefact-types.md](artefact-types.md#51-working-grid)).
6. `tract_disconnection` is the fraction of streamlines that intersect the lesion ([artefact-types.md §5](artefact-types.md#5-types-in-registry-100)).
7. Every parameter has a default; `seed` is reserved; users choose implementations, not parameter values; configured variants and bindings to `case.sex` / `case.age_band` cover the rest ([pipeline.md §5](pipeline.md#5-parameters)).
8. Provenance in two levels: the step's record is wrapped verbatim by the platform's run record, which adds digest, executor, resources, slot choice and accounting ([provenance.md](provenance.md)).
9. Exit code 10 covers both rejected input and failed QC, distinguished by `error.json.category`; error codes are declared in the manifest; non-fatal findings are `warnings` in provenance; defined contract violations ([step-contract.md §4](step-contract.md#4-exit-codes-and-errorjson)).
10. No network opt-in in contract 1.0 ([step-contract.md §2.4](step-contract.md#24-behaviour)).
11. Input sets, conditions on input presence only, conditional produced ports, ordered fallback sources, and a static availability check ([pipeline.md §2–3](pipeline.md#2-input-sets)).
12. Slot matching by port name and type (rules M1–M5); one node per slot name per pipeline ([pipeline.md §4.1](pipeline.md#41-matching-a-step-to-a-slot)).
13. Any step failure stops the run; `optional` means "may be unavailable", not "may fail" ([pipeline.md §6](pipeline.md#6-run-resolution)).
14. Implementation choice order: user, deployment default, pipeline default; stubs may be allowed but are enabled only in CI or demo deployments; a stub-only slot has no default ([pipeline.md §4, §6](pipeline.md#4-slots-and-implementations)).
15. A step name and version map to exactly one image digest ([versioning.md §3](versioning.md#3-steps)).
16. Bundle requirements match on bundle type with optional id and version range, and mount at `/ref/<requirement name>`; content digest formula; `redistributable` and `synthetic` flags; small method templates may stay in the image ([bundles.md](bundles.md), [step-contract.md §1.5](step-contract.md#15-reference-bundle-requirements)).
17. Semantic versioning rules where results-changing step changes are at least minor and patches must pass the previous reference test ([versioning.md §3](versioning.md#3-steps)).
18. `executor: external` is reserved for results uploaded through the SP2 endpoint ([provenance.md §3.2](provenance.md#32-step-records)).
19. Executor runtime settings: clean environment with `STEP_CPUS` and thread variables, `SIGTERM` then `SIGKILL` after 30 s, Docker and Apptainer flags ([step-contract.md §2.3, §6](step-contract.md#6-container-constraints-docker-and-apptainer)).
