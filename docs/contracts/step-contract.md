# Step contract 1.1

Status: normative. Contract version `1.1`. Decision records: [ADR-0001](../adr/0001-step-contract.md), [ADR-0016](../adr/0016-contract-1-1-sdk-gaps.md) (changes in 1.1).
Schemas: [`contracts/schemas/v1/step.schema.json`](../../contracts/schemas/v1/step.schema.json), [`error.schema.json`](../../contracts/schemas/v1/error.schema.json), [`provenance.schema.json`](../../contracts/schemas/v1/provenance.schema.json).
Conventions (RFC 2119 keywords, identifiers, hashes, timestamps) are defined in [README.md](README.md#conventions).

A *step* is one container image plus one manifest, `step.yaml`. The platform runs a step by staging files into `/in`, mounting reference bundles under `/ref`, invoking one fixed command and collecting `/out`. A step knows nothing about cases, users, pipelines, executors or the database.

Parties named in this document:

- **step author**: writes the step image and its manifest (cards F0-22…F0-27 and later);
- **SDK**: the helper package that implements the step side of this contract (card F0-21);
- **executor**: starts the container and collects its results (cards F0-13, F0-14, F0-16);
- **orchestrator**: plans runs, stages inputs, decides what happens after a step ends (card F0-15).

## 1. The manifest (`step.yaml`)

### 1.1 Location

1. The manifest MUST be stored in the step's source repository and MUST be copied into the image at `/opt/step/step.yaml`.
2. The platform MUST obtain the manifest of a registered implementation from the image, by digest (card F0-33's registry). The copy in the source repository is for review and for the reference test.

*Rationale:* the manifest must be bound to an immutable image digest; reading it from the image makes a mismatch between manifest and code impossible.

### 1.2 Fields

| Field | Req. | Meaning | Consumers |
|---|---|---|---|
| `contract_version` | MUST | Contract version the step implements, `"1.1"` (or `"1.0"` for a step written for 1.0, which keeps the 1.0 rules; rules marked *1.1* below apply only to steps declaring 1.1 or later). | orchestrator (compatibility), SDK |
| `kind` | MUST | `Step`. | validator |
| `name` | MUST | Step identifier, kebab-case, stable across versions. | registry, provenance, UI |
| `version` | MUST | Semantic version of the step ([versioning.md](versioning.md#3-steps)). | registry, provenance |
| `title`, `description` | MUST / MAY | Human-readable, English. | UI, report |
| `provides` | MUST | The slot name this step can fill ([pipeline.md](pipeline.md#4-slots-and-implementations)). | orchestrator, registry |
| `stub` | MAY (default `false`) | `true` for CI fixtures and placeholders that produce synthetic output. | orchestrator, UI |
| `image.repository` | MUST | Fully qualified repository, no tag or digest (`docker.io/org/name`). | registry tooling |
| `source_url` | MAY | `https://` URL of the source repository. | UI, report |
| `licence.spdx` | MUST | SPDX expression for the step's own code. | release/SOUP list, on-premise installer |
| `licence.third_party[]` | SHOULD | `{name, version, spdx}` for every major third-party component in the image; non-SPDX licences use `LicenseRef-<name>`. | release/SOUP list, redistribution decisions |
| `inputs[]` | MUST (may be empty) | Input ports ([1.3](#13-ports)). | orchestrator, SDK |
| `outputs[]` | MUST (≥ 1) | Output ports. | orchestrator, SDK, case store |
| `parameters` | MUST | Parameter schema ([1.4](#14-parameters)). | orchestrator, SDK |
| `bundles[]` | MAY | Reference bundle requirements ([1.5](#15-reference-bundle-requirements)). | orchestrator, bundle registry |
| `resources` | MUST | Resource request ([1.6](#16-resources)). | executors |
| `errors[]` | SHOULD | Declared error codes ([4.3](#43-error-codes)). | UI translations, SDK |
| `reference_test` | SHOULD (MUST for non-stub steps before they are marked validated) | Reference dataset and expected outputs ([1.7](#17-reference-test)). | step CI, nightly CI |

Unknown fields are rejected (the schemas set `additionalProperties: false`).

### 1.3 Ports

Each entry of `inputs` and `outputs` is a port: `{name, type, required, description}`.

1. `name` MUST be snake_case and unique within `inputs` (respectively `outputs`). The names `params`, `provenance`, `error` and `extra` are reserved.
2. `type` MUST be an artefact type defined in the artefact type registry ([artefact-types.md](artefact-types.md)). The type fixes the file format, extension, voxel grid where relevant, and the BIDS-style naming used by the case store.
3. `required` defaults to `true`. For inputs, `false` means the step works without it. For outputs, `true` means the step guarantees the file on success; `false` means it may or may not be produced.
4. A port carries exactly one file in contract 1.1.

*Rationale for one file per port:* every consumer (wiring, provenance, case store naming, viewer) becomes simpler; multi-file ports can be added later as a minor change.

### 1.4 Parameters

1. `parameters` MUST be a JSON Schema (draft 2020-12) with `type: object`, `additionalProperties: false` and a `properties` map. `required` MUST be absent or empty.
2. Every property MUST declare a `default`, and the defaults MUST validate against the schema. A parameter that may be "unset" uses a nullable type with `default: null`.
3. Property names MUST be snake_case. The schema MUST be self-contained (no `$ref` outside the document).
4. The name `seed` is reserved: if a step is stochastic it MUST declare `seed` with `type: integer`, and it MUST derive all randomness from it. The orchestrator sets `seed` from the run seed unless the pipeline sets it explicitly.
5. The orchestrator computes the effective parameters as: schema defaults, overridden by the pipeline implementation's literal `parameters`, overridden by `parameter_bindings` whose source value is known ([pipeline.md](pipeline.md#5-parameters)), then `seed`. It validates the result against the schema before staging and writes it, complete, to `/in/params.json`.

*Rationale for mandatory defaults:* a run must be reproducible from the manifest plus the recorded overrides, and steps must stay runnable outside the platform (reference test, lab scripts on the HPC).

### 1.5 Reference bundle requirements

Each entry of `bundles` is `{name, type, id, version, required, description}`.

1. `name` (snake_case) is the mount name: the bundle is mounted read-only at `/ref/<name>/`.
2. `type` MUST match the bundle's `type` ([bundles.md](bundles.md)). `id` MAY restrict the requirement to one bundle; `version` MAY restrict it to a range ([versioning.md](versioning.md#7-version-ranges)).
3. `required` defaults to `true`. The orchestrator MUST refuse to start a step whose required bundle cannot be resolved or fails verification. An optional bundle that cannot be resolved is simply not mounted.
4. Data that varies independently of the code (connectomes, tract atlases, concept maps, model weights, calibration tables) MUST be a bundle. Small, method-specific files that are part of the algorithm (registration templates and parameter files shipped with a tool) MAY be built into the image.

### 1.6 Resources

`resources` holds `{cpus, memory_mb, wall_time_minutes, scratch_mb, gpu}`.

1. `cpus`, `memory_mb` and `wall_time_minutes` are REQUIRED and describe what the step needs for a typical case. Executors MUST grant at least `memory_mb` and `wall_time_minutes`, and SHOULD grant exactly `cpus`.
2. `scratch_mb` (default 1024) is the space the step needs in `/tmp`.
3. `gpu.mode` is `none` (default when `gpu` is absent), `optional` (use a GPU if granted, otherwise run on CPU) or `required`. `gpu.count` defaults to 1; `gpu.memory_mb` is the minimum GPU memory. An executor that cannot grant a required GPU MUST refuse the step before starting it.
4. Executors MAY scale the request per deployment (configuration of cards F0-14 and F0-16); the granted values are recorded in provenance.

The manifest does not say which executor runs a step: that is deployment configuration (D3).

### 1.7 Reference test

`reference_test` declares the step's reference dataset and expected outputs (D1). It is run by the step's own CI (template from card F0-21) and by the nightly cross-step CI (card F1-14).

1. `inputs` maps input ports to fixture files; `bundles` maps bundle requirement names to `{id, version}`; `parameters` overrides defaults. Paths are relative to the directory containing `step.yaml` in the source repository and MUST NOT leave it. Fixtures MUST be synthetic or public with a licence that permits redistribution (plan section 6); they are not copied into the image.
2. Every required input MUST have a fixture.
3. `expected` maps output ports to a comparison:
   - `sha256`: byte-identical to `file` (use for stubs and fully deterministic text outputs);
   - `dice` with `min`: binary NIfTI overlap with `file` of at least `min`;
   - `allclose` with `rtol`/`atol`: NIfTI voxel values, or numeric TSV columns, close to `file`;
   - `json` with `rtol`/`atol`: same JSON structure as `file`, numbers compared with the tolerances;
   - `exists`: produced and of the right format.
4. A threshold or tolerance, once frozen, is part of the step's interface: loosening it is a minor version change and is recorded in the step's changelog.
5. *1.1.* Fixture files (inputs and `expected` files) MAY be generated instead of committed. `generator` then names a Python 3 script (relative path inside the step directory, ending in `.py`) and:
   - the reference-test runner (the SDK's `stroke-step-reftest`, the step CI, the nightly CI) MUST run it before staging, with the runner's own interpreter, no arguments and the step directory as working directory, and MUST fail the test if it exits non-zero or if a fixture path named in `reference_test` still does not exist;
   - the script MUST write every uncommitted fixture path named in `reference_test`, MUST NOT write outside the step directory and MUST NOT access the network; it MAY import only the SDK, its dependencies (`steps/sdk/requirements-image.txt`) and the Python standard library, and MAY read files in the repository (such as `contracts/artefact-types.yaml`);
   - it MUST be deterministic: every run on any host writes byte-identical files (fixed seeds, no time stamps; gzip headers without modification time), so the input hashes in the reference run's provenance are reproducible;
   - the generated data MUST be synthetic (plan section 6), and `expected` files MUST be computed independently of the step's code (closed-form values or an independent implementation), never by running the step;
   - generated files SHOULD be ignored by version control. A change to the generator that changes the bytes it writes is a change of the reference dataset: the step's next version follows [versioning.md](versioning.md#3-steps) as if the fixtures had been edited.

   *Rationale:* NIfTI fixtures on the working grid are large and binary; a small deterministic generator is reviewable, and byte-identity keeps "same inputs" checkable by hash.

## 2. Invocation

### 2.1 Command

The executor MUST start the container with exactly this command, ignoring any `ENTRYPOINT` or `CMD` of the image:

```
/usr/local/bin/run --in /in --out /out --params /in/params.json
```

1. The image MUST provide an executable at `/usr/local/bin/run`.
2. The step MUST read the parameter file from the path given by `--params`, not from a hard-coded location.
3. The step MUST NOT require any other argument or option. It MAY accept `--help`.

The shape is deliberately close to the BIDS Apps CLI; the paths are fixed so that every executor can stage files the same way.

### 2.2 Filesystem

| Path | Access | Content |
|---|---|---|
| `/in/<port><ext>` | read-only | One file per staged input port, named by port name plus the artefact type's extension (e.g. `/in/ct.nii.gz`, `/in/lesion_mask.nii.gz`). Inputs that are not available for this run are absent. |
| `/in/params.json` | read-only | The effective parameters (JSON object). |
| `/ref/<name>/` | read-only | One directory per resolved bundle requirement, containing the bundle's `bundle.yaml` and files. |
| `/out/` | read-write, empty at start | Outputs, `provenance.json`, `error.json`, `extra/`. |
| `/tmp/` | read-write, empty at start | Scratch space, discarded after the run. `HOME` points here. |

1. A step MUST NOT write anywhere except `/out` and `/tmp`, and MUST NOT rely on any other path being writable.
2. `/out` contains only: one file `/out/<port><ext>` per produced output port (*1.1:* only on exit 0); `/out/provenance.json`; `/out/error.json` (only on failure); and an optional directory `/out/extra/` for undeclared diagnostic files (kept with the step run, never wired to other steps, never shown as an analysis result). Any other file in `/out` is a contract violation.
3. Steps never receive subject identifiers, file names chosen by users or case metadata other than through declared parameters. Naming outputs per subject is the case store's job ([artefact-types.md](artefact-types.md#4-naming-in-the-case-store)).

*Rationale for fixed port-based file names:* steps can be written and tested without any knowledge of BIDS naming or of the case, and no identifier ever enters a container.

### 2.3 Environment

The executor MUST provide a clean environment containing the image's own variables plus:

| Variable | Value |
|---|---|
| `HOME`, `TMPDIR` | `/tmp` |
| `TZ` | `UTC` |
| `STEP_CPUS` | granted CPU count |
| `OMP_NUM_THREADS`, `MKL_NUM_THREADS`, `OPENBLAS_NUM_THREADS`, `ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS` | granted CPU count |
| `STEP_MEMORY_MB` | granted memory |
| `STEP_GPUS` | granted GPU count (`0` if none) |

A step MUST NOT use more threads than `STEP_CPUS` and MUST NOT depend on any other host variable. The working directory is unspecified; steps MUST use absolute paths.

### 2.4 Behaviour

A step:

1. MUST NOT access the network. Executors disable networking where the runtime allows it; steps MUST NOT depend on whether it is disabled.
2. MUST run as an arbitrary non-root UID and GID that has no entry in `/etc/passwd`, and MUST NOT need privilege escalation, capabilities or `setuid` binaries.
3. MUST NOT start containers (no Docker-in-Docker, no nested Apptainer).
4. MUST be deterministic: given the same image digest, inputs, effective parameters (including `seed`), bundles and `STEP_CPUS`, outputs MUST be identical or within the tolerances of the step's reference test. Results MUST NOT depend on wall-clock time, host name, locale or directory listing order.
5. MUST validate its inputs (format, type constraints from the registry such as binary values and voxel grid) before doing work, and reject invalid input with exit code 10. *1.1:* a missing required input, an input that fails its artefact-type checks and parameters that fail the parameter schema are reported with the reserved codes of [4.4](#44-reserved-error-codes).
6. SHOULD handle `SIGTERM` by stopping promptly. Executors send `SIGTERM` on cancellation or timeout and `SIGKILL` 30 seconds later; `/out` of an interrupted step is discarded. *1.1:* after `SIGTERM` a step that still exits by itself SHOULD write, within the 30 seconds and if it can, `error.json` with category `internal` and the reserved code `terminated` and `provenance.json` with `status: failed`, and SHOULD exit with code `143` (128 + 15, the code a shell reports for death by `SIGTERM`); it SHOULD NOT exit with `0` or `10` once it has received `SIGTERM`. The executor records the outcome from its own action (`canceled` or `timed_out`, [provenance.md](provenance.md#32-step-records)), whatever the exit code and files; the reports are kept for diagnosis only.
7. Is always started with an empty `/out`. A retry is a new start with a new empty `/out`; steps need no resume logic.

## 3. Logging

1. Steps write logs to standard output and standard error as UTF-8 text lines. Executors capture both streams in order and store them with the step run.
2. Lines SHOULD start with a level word (`DEBUG`, `INFO`, `WARNING`, `ERROR`).
3. Logs MUST NOT contain file contents, image data or anything identifying; they MAY contain the step's port names and parameter values.
4. Executors MAY truncate a step's log beyond 10 MiB, keeping the beginning and the end.

## 4. Exit codes and `error.json`

### 4.1 Exit codes

| Code | Meaning | Required files in `/out` | Platform action |
|---|---|---|---|
| `0` | Success | every `required: true` output; `provenance.json` with `status: succeeded` | Collect outputs; continue the run. |
| `10` | Input rejected or QC failure: the input is unsuitable, retrying will not help | `error.json` with category `input_rejected` or `qc_failed`; `provenance.json` with `status: rejected`; *1.1:* no output port files, `provenance.outputs` empty | Stop the run; show `error.json.message` to the user; no automatic retry. |
| any other, or killed by a signal | Internal failure (`143` after `SIGTERM`, 2.4 item 6) | `error.json` with category `internal` MAY be written; `provenance.json` SHOULD be written with `status: failed` (*1.1:* with `outputs` empty); output port files SHOULD be removed and are never collected | Stop the run; mark the step failed; automatic retry per orchestrator policy. |

The orchestrator MUST treat the following as an internal failure with outcome `contract_violation`: exit 0 with a missing required output, a missing or schema-invalid `provenance.json`, a file in `/out` that is not allowed by 2.2, or an output whose format does not match its artefact type; exit 10 without a valid `error.json`.

*Rationale for one rejection code:* the platform's only decision is "show the message and stop, do not retry"; the distinction between invalid input and a failed QC check is carried by `error.json.category` for the UI.

### 4.2 `error.json`

Fields: `contract_version`, `kind: StepError`, `step {name, version}`, `category` (`input_rejected`, `qc_failed`, `internal`), `code`, `message`, optional `port`, optional `details`.

1. `message` is shown to users. It MUST be one or two plain English sentences, at most 500 characters, and MUST NOT contain file system paths or identifiers. Translation is done by the UI keyed on `code`.
2. `code` is snake_case. With exit code 10 it MUST be one of the step's declared `errors[].code` or (*1.1*) a reserved rejection code ([4.4](#44-reserved-error-codes)), with the declared category.
3. `port` names the input port concerned, if any.
4. `details` is a flat object of scalar values for administrators and logs (e.g. measured volume, threshold).

### 4.3 Error codes

`errors[]` in the manifest declares every code a step can return with exit 10: `{code, category, description}`, apart from the reserved codes of 4.4, which every step declaring contract 1.1 or later declares implicitly. The UI uses them to build translation catalogues; the SDK refuses to emit an undeclared code. Codes are part of the step's interface ([versioning.md](versioning.md#3-steps)). Codes of category `internal` are not declared.

### 4.4 Reserved error codes

*1.1.* These codes are defined by the contract, have the same meaning in every step and are translated once by the UI:

| Code | Category | Exit | Meaning | `port` |
|---|---|---|---|---|
| `input_missing` | `input_rejected` | 10 | A required input port has no file in `/in`. | the port |
| `input_format_invalid` | `input_rejected` | 10 | An input file fails the checks of its artefact type (format, extension, voxel grid, binary values, JSON schema, TSV columns). | the port |
| `invalid_parameters` | `input_rejected` | 10 | The parameter file does not satisfy the step's parameter schema. | absent |
| `terminated` | `internal` | 143 | The step stopped after `SIGTERM` (2.4 item 6). | absent |

1. A step declaring contract 1.1 or later MUST use these codes for these conditions, MAY return the three rejection codes without listing them in `errors[]`, and MAY list them (for instance with a step-specific description); a listed reserved code MUST have the category above. A reserved code MUST NOT be used with another category or meaning.
2. Content checks beyond the artefact type (an empty mask, a CT without skull) use the step's own declared codes.
3. A step declaring contract 1.0 MUST declare any of these codes it returns with exit 10, as in 1.0.
4. `contracts/validate.py` applies these rules to manifests and `error.json` files, and, when an `error.json` is given together with its manifest, checks that an exit-10 code is declared or reserved and has the declared category.

*Rationale:* these three rejections are detected by the SDK before step code runs, identically for every step; one shared code each keeps the UI catalogue small and spares every manifest three boilerplate entries.

## 5. `provenance.json`

Every step MUST write `/out/provenance.json` (kind `StepProvenance`) on exit 0 and exit 10, and SHOULD write it on internal failure. It records what the step itself knows: step identity, status, timings, effective parameters (on rejection or failure, the parameters as far as they were read), SHA-256 and size of every input and output file (*1.1:* no outputs unless `status: succeeded`), the bundles read from `/ref/*/bundle.yaml`, key software versions and warnings. Facts the step cannot know (image digest, executor, granted resources, slot and implementation, accounting) are added by the platform in the run-level record. Full definition: [provenance.md](provenance.md).

Non-fatal findings (for example, a small lesion volume) are reported as `warnings[]` `{code, message, port}` in `provenance.json`; they do not change the exit code.

## 6. Container constraints (Docker and Apptainer)

A step image MUST satisfy all of the following, so that the same image digest runs unchanged under Docker (local executor) and Apptainer (HPC executor):

1. **Platform.** `linux/amd64`. The image is referenced by OCI digest; Apptainer images are built from that digest (`apptainer build step.sif docker://<repo>@sha256:…`) and the SIF file's SHA-256 is recorded in provenance.
2. **Read-only root.** The step MUST work with a read-only root filesystem (`docker run --read-only`; SIF images are read-only). Nothing may be written to the image at run time, including caches (`~/.cache`, Matplotlib and Numba caches, `__pycache__`): direct them to `/tmp` or pre-build them.
3. **Mount points.** The image MUST contain empty directories `/in`, `/out`, `/ref` and `/tmp`, because Apptainer cannot always create bind targets.
4. **Arbitrary user.** The step runs under an arbitrary UID/GID (`docker run --user <uid>:<gid>`; under Apptainer the submitting user). All files in the image MUST be world-readable, and executables world-executable. No `USER`-specific home directory may be assumed.
5. **No privileges.** Executors run Docker with `--cap-drop ALL --security-opt no-new-privileges --network none`, a PID limit, and without `--privileged`; the step MUST work under these settings. Apptainer runs with `--containall --cleanenv --no-home` and explicit `--bind` mounts; networking is isolated where the site allows it.
6. **Entry point.** `ENTRYPOINT`, `CMD`, `WORKDIR` and `USER` of the image are ignored. Variables set with `ENV` are kept (Apptainer imports them at build time).
7. **GPU.** CUDA user-space libraries are inside the image; the driver comes from the host (`docker --gpus`, `apptainer --nv`).
8. **No nested containers** and no access to a container runtime socket.

## 7. Registration checks

When an implementation is registered (card F0-33) the platform MUST: extract `/opt/step/step.yaml` from the image by digest; validate it against the schema and the rules of this document with `contracts/validate.py` or an equivalent; check its `contract_version` ([versioning.md](versioning.md#2-the-contract-version)); and check that `name` and `version` match the pipeline's implementation entry. Non-stub steps SHOULD have passed their reference test in CI for that digest.
