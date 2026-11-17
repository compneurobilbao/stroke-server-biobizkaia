# Provenance records 1.2 (`provenance.json`)

Status: normative. Contract version `1.2` (`RunProvenance` gains the plan record and declared input origins in 1.2, [ADR-0018](../adr/0018-presets-free-mode-external-tools.md); `StepProvenance` is unchanged since 1.1). Schema: [`contracts/schemas/v1/provenance.schema.json`](../../contracts/schemas/v1/provenance.schema.json).
Worked examples: [`provenance-step.json`](../../contracts/examples/provenance-step.json), [`provenance-step-rejected.json`](../../contracts/examples/provenance-step-rejected.json), [`provenance-run.json`](../../contracts/examples/provenance-run.json), [`provenance-run-plan.json`](../../contracts/examples/provenance-run-plan.json) (1.2, a run created from a preset).

There are two kinds of record, in one schema:

| Kind | Written by | Where | What it knows |
|---|---|---|---|
| `StepProvenance` | the step (via the SDK) | `/out/provenance.json` | what happened inside the container |
| `RunProvenance` | the platform (orchestrator, case store) | the run's derivatives root (card F0-17) | everything about the run, including each step record verbatim |

*Rationale for the split:* a container cannot reliably know its own image digest, the executor, the resources it was granted, the slot it filled or who is paying. Asking steps to report these would make them lie or depend on the platform. The platform adds them around the step's record and never edits that record, so the step's file can be checked by hash.

## 1. Common rules

1. Timestamps are RFC 3339 in UTC with a literal `Z`, at most microsecond precision.
2. File hashes are lowercase hexadecimal SHA-256 of the file bytes (`sha256`); image and bundle digests are `sha256:<hex>` strings.
3. Records MUST NOT contain names, e-mail addresses, file names chosen by users, host names, paths on the host or any case metadata other than parameter values. The only identity is the opaque `accounting.submitted_by`.

## 2. `StepProvenance`

| Field | Meaning |
|---|---|
| `contract_version` | Same as the step manifest's. |
| `kind` | `StepProvenance` |
| `step` | `{name, version}` from the manifest. |
| `status` | `succeeded` (exit 0), `rejected` (exit 10) or `failed` (internal failure, best effort). |
| `started_at`, `finished_at` | The step's own clock, start of `run` to the moment the record is written. |
| `parameters` | With `status: succeeded`: the effective parameters as read and validated by the step (after applying defaults). With `rejected` or `failed`: the same if validation had passed; otherwise (code `invalid_parameters`) the object exactly as read from the parameter file, unvalidated; `{}` if the file could not be read. |
| `inputs[]` | `{port, type, sha256, size_bytes}` for every staged input file. With `rejected` or `failed` it MAY list fewer (the step stopped before hashing them). |
| `outputs[]` | `{port, type, sha256, size_bytes}` for every produced output file (not for `provenance.json`, `error.json` or `extra/`). *1.1:* empty unless `status: succeeded`; outputs are collected only after exit 0. |
| `bundles[]` | `{name, id, version, content_digest}` copied from each mounted `/ref/<name>/bundle.yaml`. A model is a bundle (D10), so the model version appears here. |
| `software[]` | `{name, version}` of the tools and libraries that determine results (e.g. FSL, elastix, MRtrix3). Names and versions are free-form strings and are not checked by the validator; use the upstream project name. A step built on the SDK records `compneuro-step-sdk` (SDK version) and `python` (interpreter version) automatically; step code adds the rest. |
| `warnings[]` | Optional `{code, message, port}` non-fatal findings, shown in the UI and report. |

The orchestrator, when collecting, MUST validate the record against the schema, check that `step` matches the implementation it started, recompute the SHA-256 of every staged input and collected output and compare them with the record, and check bundle digests against what it mounted. A mismatch is a `contract_violation`. The record's `parameters` are checked against the manifest's parameter schema only when `status` is `succeeded`; for `rejected` and `failed` they are kept as evidence of what the step received.

*Rationale for raw parameters on rejection:* the record must say honestly what the step was given; recording defaults instead would describe a run that did not happen.

## 3. `RunProvenance`

| Field | Meaning | Consumers |
|---|---|---|
| `run_id`, `case_id` | Opaque platform UUIDs. | case store, API |
| `pipeline` | `{id, version, sha256}`; `sha256` of the exact `pipeline.yaml` bytes used. | report, audit |
| `platform` | `{version, artefact_types_version}`: platform release and artefact type registry version. | audit |
| `status` | `running`, `succeeded`, `failed`, `canceled`. `finished_at` is present unless `running`. | API, report |
| `created_at`, `finished_at` | Run timestamps. | API |
| `seed` | Run seed (unsigned 32-bit), passed to every step that declares `seed`. Kept on resume and retry. | reproducibility |
| `input_set` | Name of the matched input set. | report, UI |
| `inputs[]` | `{name, type, sha256, size_bytes, origin}` of the run inputs; `origin` ([3.3](#33-declared-input-origins), *1.2*) is optional. | audit, report |
| `plan` | *1.2*, optional: the run's plan request as resolved ([3.4](#34-plan-record)). | report, UI, "run again with the same plan" |
| `accounting` | Optional `{submitted_by, submit_as, billing_account}` ([4](#4-accounting)). | HPC usage reports |
| `slots[]` | One record per pipeline slot, in pipeline order ([3.1](#31-slot-records)). | report, UI, audit |
| `steps[]` | One record per step execution attempt ([3.2](#32-step-records)). | report, UI, audit |

### 3.1 Slot records

`{slot, status, reason, plan_role, implementation, bundles}`:

- `status: selected` with `implementation {id, step {name, version}, image, stub, chosen_by, upstream}` and the resolved `bundles[]`; `chosen_by` is `user`, `deployment_default`, `pipeline_default` or (*1.2*) `preset` (D15: "the choice, image digest and model bundle are recorded in provenance"). *1.2:* `upstream` is a copy of the step manifest's `upstream` block ([step-contract.md §1.8](step-contract.md#18-external-tools-the-upstream-block)) and is present exactly when the manifest has one.
- `status: skipped` with `reason` `condition_false`, `upstream_absent` or (*1.2*) `not_requested` (the slot is outside the run plan, [presets.md §4](presets.md#4-static-plan-closure)).
- `status: unavailable` with `reason: no_implementation`.
- *1.2:* `plan_role` (`target`, `upstream`, `if_available`, [presets.md §4.1](presets.md#41-plan-roles)) says why the slot is in the plan.

Rules for records declaring 1.2 (checked by `contracts/validate.py`):

1. A record with `plan` gives every slot a `plan_role` except slots with reason `condition_false` or `not_requested`, which have none. A slot that is `unavailable` or `skipped` with `upstream_absent` has role `if_available` (a `target` or `upstream` slot without an implementation refuses the run).
2. `plan_role`, reason `not_requested` and `chosen_by: preset` need `plan`; `not_requested` needs `plan.mode` `preset` or `free` and status `skipped`; `chosen_by: preset` needs `plan.mode: preset`.
3. Records declaring 1.0 or 1.1 MUST NOT use `plan`, `plan_role`, `not_requested`, `chosen_by: preset`, `implementation.upstream` or `inputs[].origin`.

### 3.2 Step records

`{slot, execution, provenance, error}`:

- `execution` is written by the executor: `executor` (`subprocess`, `docker`, `slurm`, `external`), `runtime` (`docker`, `apptainer`, `none`), `image` (REQUIRED for `docker` and `slurm`), optional `sif_sha256`, optional `external_job_id` (e.g. the Slurm job ID), `attempt` (1-based), `queued_at`, `started_at`, `finished_at` (each present once known; a job canceled while queued has no `started_at`), `exit_code` (null if the process never ran or was killed), `outcome` (`succeeded`, `rejected`, `failed`, `canceled`, `timed_out`, `contract_violation`), granted `resources {cpus, memory_mb, gpus, wall_time_minutes}`, optional `usage {wall_seconds, cpu_core_hours, gpu_hours, max_rss_mb}`.
- `provenance` is the step's `StepProvenance`, verbatim, or `null` if the step wrote none.
- `error` is the step's `StepError` ([step-contract.md](step-contract.md#42-errorjson)), verbatim, or `null`. It MUST be present when `outcome` is `rejected`.

`executor: external` is reserved for results that were computed outside the platform and uploaded through a service-account endpoint (D8, path 2); `runtime` is then `none` and `provenance` holds whatever step record accompanied the upload.

The legacy `subprocess` executor (dummy and anatproc processors during migration) records `runtime: none`, no `image`, and usually `provenance: null`.

### 3.3 Declared input origins

*1.2.* A run input MAY carry the origin its uploader declared: `{kind: manual}` (drawn by hand) or `{kind: external-tool, tool, version}`, where `tool` and `version` match `[A-Za-z0-9._+-]{1,64}` (no free text, which could carry identifiers). The platform cannot verify a declared origin; UI and report present it as "declared by the user". The origin `platform-run` (an artefact of an earlier run) is reserved for a later minor version.

### 3.4 Plan record

*1.2.* `plan` is `{mode, preset, targets}`:

| Field | Meaning |
|---|---|
| `mode` | `full` (no preset and no targets: rule R0 of [presets.md §4](presets.md#4-static-plan-closure)), `preset` or `free` (targets chosen by the user). |
| `preset` | `{id, version, sha256}` of the preset (SHA-256 of the exact file bytes) when `mode` is `preset`; `null` otherwise. |
| `targets` | The request as resolved: `{required: [slot.port], if_available: [slot.port]}`. Empty (`{}`) for `full`; at least one required target for `preset`; at least one target for `free`. A port is not both required and if available. |

Against the pipeline, every target is a produced port, and the slot of every required target has `plan_role: target`. Together with the pipeline SHA-256 and the slot records, the plan record is enough to re-submit the same request and to explain why each slot ran or did not.

## 4. Accounting

`accounting` is the same object in `RunProvenance` and in the executor request of card F0-13:

| Field | Meaning |
|---|---|
| `submitted_by` | `user:<opaque id>` or `service:<opaque id>`; never an e-mail address or a name. |
| `submit_as` | Policy under which jobs were submitted: `service-account` (the only one implemented in Phase 0), `per-user`, `chargeback`. |
| `billing_account` | Account or project billed on the HPC (e.g. the Slurm `--account`), or `null`. |

Per-step resource usage (core-hours, GPU-hours) is in `execution.usage`, so that chargeback can be built later without a schema change (F0-16).

## 5. Run-level file in the case store

The case store (card F0-17) writes the `RunProvenance` file next to the run's derivatives, updates it after each step, and also stores each step's original `/out/provenance.json` unchanged inside that step's derivatives directory. The report step receives the record through the `run.provenance` source ([pipeline.md](pipeline.md#31-sources)), with `status: running`.
