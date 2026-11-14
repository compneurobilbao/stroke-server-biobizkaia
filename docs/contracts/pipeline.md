# Pipeline definition 1.1 (`pipeline.yaml`)

Status: normative. Contract version `1.1` (this document is unchanged since 1.0). Decision records: [ADR-0002](../adr/0002-declarative-pipelines-own-orchestrator.md), [ADR-0015](../adr/0015-selectable-implementations-per-slot.md).
Schema: [`contracts/schemas/v1/pipeline.schema.json`](../../contracts/schemas/v1/pipeline.schema.json). Worked example: [`contracts/examples/pipeline-stroke-lnm.yaml`](../../contracts/examples/pipeline-stroke-lnm.yaml).

A pipeline is a small directed acyclic graph of **slots**. A slot is a named position defined by the artefact types it consumes and produces; any step whose manifest declares `provides: <slot>` and matches the slot's ports can fill it. The pipeline file lists, per slot, the implementations that are *allowed* (by image digest); a deployment *enables* a subset of them; the user *chooses* one per slot when creating a run.

Consumers: orchestrator (card F0-15), run API and admin endpoints (F0-30), case wizard (F0-31), pipeline registry and CI configuration (F0-33).

## 1. Top-level fields

| Field | Req. | Meaning |
|---|---|---|
| `contract_version`, `kind: Pipeline` | MUST | See [versioning.md](versioning.md). |
| `id`, `version` | MUST | Kebab-case identifier and semantic version ([versioning.md](versioning.md#4-pipelines)). |
| `title`, `description` | MUST / MAY | Shown in the case wizard. |
| `inputs[]` | MUST | Run inputs `{name, type, description}`. `name` is the role of the case's input file (D4 `InputFile.role`, e.g. `ct`, `lesion_native`, `lesion_mni`); `type` is an artefact type. |
| `input_sets[]` | MUST | Accepted combinations of inputs `{name, inputs[], description}` ([2](#2-input-sets)). |
| `slots[]` | MUST | The slots, in display order ([3](#3-slots-and-wiring)). |

## 2. Input sets

1. A run is created with a set of input files. The set of their roles MUST equal exactly one `input_sets[].inputs`; otherwise the run is refused. The matching input set's `name` is recorded in provenance.
2. Two input sets MUST NOT contain the same inputs.
3. Input sets are what the case wizard presents as modes (for `stroke-lnm`: "CT and native mask", "MNI mask only").

## 3. Slots and wiring

Each slot has:

| Field | Req. | Meaning |
|---|---|---|
| `name` | MUST | Kebab-case, unique in the pipeline; also the value of `provides` in matching step manifests. `inputs` and `run` are reserved. |
| `title`, `description` | MAY | UI text. |
| `optional` | MAY (default `false`) | Whether the run may proceed without this slot ([6](#6-run-resolution)). |
| `when` | MAY | Condition on the run's inputs ([3.2](#32-conditions)). |
| `consumes[]` | MUST | Ports `{port, type, from, optional}`. |
| `produces[]` | MUST | Ports `{port, type, optional, when}`. |
| `implementations` | MUST | `{default, allowed[]}` ([4](#4-slots-and-implementations)). |

A pipeline can contain each slot name only once in contract 1.1.

### 3.1 Sources

`consumes[].from` is a source or a list of sources:

- `inputs.<name>`: a run input;
- `<slot>.<port>`: a port produced by another slot;
- `run.provenance`: the run's `RunProvenance` record as it stands when the consuming step is staged (type `run_provenance`; used by the report step).

A list means *the first source in list order that is available at run time*. This is how a slot reads the MNI mask from normalisation in one input set and from intake in the other.

Rules (checked by `contracts/validate.py`):

1. Every source MUST exist, and its type MUST equal the consuming port's `type`.
2. A slot MUST NOT consume its own ports, and the graph induced by sources MUST be acyclic.
3. A `consumes` port with `optional: true` may be absent at run time; the step then simply does not receive that file.

### 3.2 Conditions

`when` holds `inputs_present: [names]` and/or `inputs_absent: [names]`; all listed clauses must hold. Conditions refer only to run inputs, so they are evaluated once, at run creation, and the set of steps a run will attempt is known before it starts.

- On a slot, a false condition skips the slot (status `skipped`, reason `condition_false`).
- On a produced port, `when` declares that the port is produced exactly when the condition holds (for example intake produces `lesion_mask_mni` only when an MNI mask was supplied). A port without `when` and without `optional: true` is produced whenever the slot succeeds.

*Rationale:* conditions over input presence cover the plan's only case (skip normalisation when the input is already an MNI mask) and keep the run plan static, which the UI timeline, resume and provenance need. Conditions on intermediate results can be added in a later minor version.

### 3.3 Static availability check

For every input set, the validator (and the orchestrator when loading a pipeline) evaluates, in topological order, whether each slot runs *definitely*, *possibly* (optional slots, and slots depending on them) or *never*, and whether each produced port is available *definitely*, *possibly* or *never*. The pipeline is invalid if:

1. a non-optional slot that can run under an input set has a required (`optional: false`) consumed port that is not *definitely* available; or
2. any slot that can run has a required consumed port that is *never* available; or
3. a slot cannot run under any input set.

## 4. Slots and implementations

```yaml
implementations:
  default: ct-lesion-to-mni152        # optional; must be an allowed id
  allowed:
    - id: ct-lesion-to-mni152          # unique within the slot
      step: {name: ct-lesion-to-mni152, version: 1.0.0}
      image: docker.io/compneurobilbaolab/ct-lesion-to-mni152-step@sha256:<64 hex>
      parameters: {...}                # optional literal overrides
      parameter_bindings: {age_band: case.age_band}   # optional
      bundles: {connectome: {id: ..., version: 1.0.0}} # optional pins
```

1. `image` MUST be pinned by digest. Tags are not accepted anywhere in a pipeline.
2. Within one pipeline, a given step `name`+`version` MUST always be pinned to the same image. The same image MAY appear in several implementation entries of a slot with different parameters (a *configured variant*, e.g. with and without ventricle removal).
3. `default`, if present, MUST be one of the allowed ids. A slot whose only implementations are stubs SHOULD have no default.
4. Stubs (`stub: true` in the manifest) MAY be listed as allowed so that CI configurations can enable them; a deployment MUST NOT enable a stub unless it is explicitly configured as a CI or demo deployment, and the UI MUST label any stub result as synthetic.

### 4.1 Matching a step to a slot

A step manifest can fill a slot when all of the following hold (rule identifiers are used in validator messages):

- **M1** `provides` equals the slot name.
- **M2** For every `produces` port of the slot, the step has an output with the same name and type. If the slot port has neither `optional: true` nor `when`, the step output MUST have `required: true`.
- **M3** Every step input with `required: true` has a `consumes` port of the same name that is not optional.
- **M4** Every step input that has a same-named `consumes` port has the same type. Consumed ports that the step does not declare are not staged. Step inputs without a same-named port are never staged (they must therefore be optional, by M3).
- **M5** The step's `contract_version` has the pipeline's major version.

Step outputs that the slot does not list are collected and stored but cannot be wired.

*Rationale for matching by port name and type:* names keep wiring explicit when a slot consumes two ports of the same type; types make mismatches detectable before anything runs.

## 5. Parameters

1. `parameters` sets literal values for the implementation; they MUST validate against the step's parameter schema.
2. `parameter_bindings` maps a parameter to case metadata. Contract 1.1 defines two sources:
   - `case.sex`: `"female"` or `"male"`;
   - `case.age_band`: a string `"<lo>-<hi>"` in whole years (e.g. `"65-69"`), as stored with the case (D4).
   If the case has no value, the binding is skipped and the default (or literal) applies. A parameter MUST NOT be both literal and bound. The bound parameter's schema MUST accept every possible source value.
3. Users do not set step parameters at run creation in contract 1.1: they choose an implementation per slot, and configured variants (4, item 2) cover the useful alternatives.

*Rationale:* keeping user input to "pick an implementation" keeps runs comparable and the provenance short; per-run parameter overrides can be added as a minor change if a real need appears.

## 6. Run resolution

At run creation the orchestrator, for each slot in topological order:

1. If `when` is false: status `skipped`, reason `condition_false`.
2. Otherwise, if a required consumed port has no available source because upstream slots were skipped or unavailable: status `skipped`, reason `upstream_absent` (only possible for optional slots, by 3.3).
3. Otherwise it chooses the implementation: the user's choice if it is enabled in the deployment; else the deployment default; else the pipeline `default` if enabled. The choice MUST be one of `allowed`, and `chosen_by` records which rule applied.
4. If no implementation is enabled: an optional slot gets status `unavailable`, reason `no_implementation`, and downstream optional slots that need it are skipped; a non-optional slot makes the run impossible, and run creation is refused with a message naming the slot.
5. Bundles are resolved per requirement: a pin in the implementation entry, else a pin in the deployment configuration, else the highest installed version of a matching bundle that satisfies the range.

Execution: slots run in dependency order; independent slots MAY run concurrently. A step that ends with any outcome other than `succeeded` stops the run (no new steps are started; running independent steps are allowed to finish); the user may retry the failed step. Per-slot failure tolerance (`on_failure`) is not part of contract 1.1.

## 7. Deployment configuration (not part of this file)

What is enabled, deployment defaults, bundle pins per deployment, executor per step and resource scaling live in the deployment's registry configuration (card F0-33) and the executor configuration (F0-14, F0-16). They MAY only narrow what `pipeline.yaml` allows. The `ci` configuration of F0-33 is such a configuration that enables stubs.
