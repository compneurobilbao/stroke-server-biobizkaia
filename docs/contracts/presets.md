# Presets and run plans 1.2 (`presets/<id>.yaml`)

Status: normative. Contract version `1.2` (new in 1.2). Decision record: [ADR-0018](../adr/0018-presets-free-mode-external-tools.md); design: [run-plans.md](../design/run-plans.md).
Schema: [`contracts/schemas/v1/preset.schema.json`](../../contracts/schemas/v1/preset.schema.json). Worked example: [`contracts/examples/preset-mni-mask-analysis.yaml`](../../contracts/examples/preset-mni-mask-analysis.yaml); its plan record: [`provenance-run-plan.json`](../../contracts/examples/provenance-run-plan.json).

A run is created from a pipeline ([pipeline.md](pipeline.md)) plus a **plan request**: a set of **targets**, each *required* or *if available*. The orchestrator closes the targets over the pipeline's wiring for the run's input set ([4](#4-static-plan-closure)); slots outside the closure are skipped with reason `not_requested`. A **preset** is a reviewed, versioned plan request stored as a file; **free mode** is a plan request whose targets the user chooses (allowed by deployment policy, not part of this contract). A request with neither a preset nor targets is the **full plan** (rule R0), the behaviour of contract 1.1.

A preset does not define slots, wiring or images; it selects inside a reviewed pipeline. Consumers: orchestrator (card F1-19, the `PresetRegistry`), pipeline registry check (F1-20), API (F1-21), case wizard (F1-22), report (F1-25).

## 1. Location

1. One file per preset, next to the pipeline it refers to: `pipelines/<pipeline id>/presets/<preset id>.yaml`.
2. Presets are added and changed only through reviewed pull requests; they are never created at run time.
3. A run records the preset `id`, `version` and the SHA-256 of the exact file bytes (`plan.preset` in [provenance.md](provenance.md#3-runprovenance)).

*Rationale for one file per preset, outside `pipeline.yaml`:* adding or changing a preset must not change the pipeline's SHA-256 and version, to which runs and resume are bound, and a deployment must be able to enable presets independently of implementations.

## 2. Fields

| Field | Req. | Meaning |
|---|---|---|
| `contract_version`, `kind: Preset` | MUST | `contract_version` is `"1.2"` or later (rule P1). |
| `id` | MUST | Kebab-case, unique per pipeline id. |
| `version` | MUST | Semantic version of the preset ([6](#6-versions)). |
| `title`, `description` | MUST / MAY | Shown in the case wizard. |
| `pipeline.id` | MUST | Id of the pipeline the preset selects in. |
| `pipeline.versions` | MUST | Range of pipeline versions the preset was written for, in the syntax of [versioning.md §7](versioning.md#7-version-ranges) (comma-separated comparators, e.g. `">=0.1.0, <0.2.0"`). |
| `input_sets[]` | MUST (≥ 1, unique) | Input sets of the pipeline that the preset accepts. |
| `targets.required[]` | MUST (≥ 1, unique) | Targets that must be produced; the run is refused otherwise. |
| `targets.if_available[]` | MAY (unique) | Targets produced when possible (best effort, like optional slots). |
| `implementations` | MAY | Per slot, exactly one of `{default: <id>}` (preset default; the user may choose another enabled implementation) or `{fixed: <id>}` (the user may not change it). |

A **target** is written as:

- a slot name (`tract-disconnection`): the slot and its *guaranteed* produced ports (no `optional: true`, and `when`, if any, holds under the input set); as an if-available target, a slot without guaranteed ports stands for the ports it may produce;
- a produced port `<slot>.<port>` (`normalisation.lesion_mask_mni`);
- an artefact type (`tract_disconnection`): the single produced port of that type whose slot can run and whose `when` holds under the input set.

```yaml
contract_version: "1.2"
kind: Preset
id: mni-mask-analysis
version: 1.0.0
title: Disconnection analysis of an MNI lesion mask
pipeline: {id: stroke-lnm, versions: ">=0.1.0, <0.2.0"}
input_sets: [mni-mask]
targets:
  required: [tract-disconnection, functional-disconnection, report]
  if_available: [cognitive-profile]
implementations:
  report: {fixed: research-report}
```

## 3. Validation rules

Checked by `contracts/validate.py` (P1 and P7 on the document alone; P2-P8 when the pipeline is given on the same command line) and by the orchestrator when it loads presets. An invalid preset is logged and not offered, never half-loaded.

| Rule | Check |
|---|---|
| P1 | The document validates against `preset.schema.json` and declares contract 1.2 or later. |
| P2 | `pipeline.id` names a loaded pipeline; the preset is attached to every loaded version of it inside `pipeline.versions`, and validated against each of them separately (P3-P7). A version for which validation fails does not offer the preset; the others still do. |
| P3 | `input_sets` are input sets of that pipeline version. |
| P4 | Every target resolves ([4](#4-static-plan-closure), step 2) under every listed input set: unknown or ambiguous targets are errors; a required target must apply and be guaranteed; an if-available target must apply (its slot's and port's `when` hold). |
| P5 | For every listed input set, the plan is feasible when every allowed implementation is assumed enabled (static check; enablement is checked again at run creation). |
| P6 | `implementations` keys are slots of the plan for at least one listed input set; values name allowed implementations of the slot; a slot has `default` or `fixed`, not both; a stub (`stub: true` in its manifest) cannot be `fixed`. |
| P7 | A slot both in `required` and in `if_available` (as written, or after resolution: `conflicting_targets`) is an error. |
| P8 | `(pipeline id, id, version)` identifies one file; a second file with the same identity is refused, and a changed file with the same version is refused at load (the SHA-256 recorded in runs would otherwise stop matching what the version means). |

The validator reports a fixed implementation whose manifest is not supplied, and a preset with no supplied pipeline version in its range, as notes (errors with `--strict`), as it does for implementations of a pipeline.

## 4. Static plan closure

`plan_closure(pipeline, input_set, required, if_available, enabled)` in `contracts/validate.py` is the reference implementation: a pure function over a valid pipeline document that the orchestrator imports. It returns, per slot, the status (`planned`, `skipped`, `unavailable`), the reason and the plan role, the resolved targets, and every refusal reason found (not only the first). Choosing among the usable implementations (step 6 of the design), parameters and bundles stays with the orchestrator ([pipeline.md §6](pipeline.md#6-run-resolution)).

Inputs: pipeline `P` (one version), input set `I` (pipeline.md §2), targets `T_req` and `T_opt`, and per slot the implementations the run can use: those the deployment enables and the choice rule of pipeline.md §6 item 3 can select (every allowed one for rule P5).

- **R0 (full plan).** When no plan is requested (no preset and no targets: both lists absent), `T_req` is every non-optional slot whose `when` holds under `I` and `T_opt` every optional slot whose `when` holds. The resulting statuses, and whether the run is refused, are exactly those of the run resolution of [pipeline.md §6](pipeline.md#6-run-resolution) (a property tested against the orchestrator's implementation). A request that gives targets, even empty lists, is not R0.

1. **Static availability.** Evaluate pipeline.md §3.3 for `I`: per slot and per produced port *definitely*, *possibly* or *never*.
2. **Resolve targets** ([2](#2-fields)) to slots and `slot.port` lists. A slot name stands for its guaranteed ports; as an if-available target, a slot without guaranteed ports stands for the ports it may produce under `I`, so that the plan record never loses a target. Refusals: `unknown_target` (no such slot, port, or produced type under `I`); `ambiguous_target` (an artefact type with several candidate ports, or a name that is both a slot and a produced artefact type; the message lists the candidates); for required targets only, `target_not_applicable` (the slot's or port's `when` is false under `I`) and `target_not_guaranteed` (the port has `optional: true`, or the slot has no guaranteed port; the same target is accepted as if available). An if-available target that does not apply is ignored. A slot named, after resolution, by both a required and an if-available target is refused with `conflicting_targets` (rule P7 for presets, applied to every request). A request whose targets leave nothing to plan is refused with `empty_request`.
3. **Close.** A work list, first-in first-out, starts with the slots of the required targets in the order written (role `target`). For each slot taken from it and each consumed port without `optional: true`, the sources of its `from` list are considered in order. A source *qualifies* under `I` when it is `inputs.<name>` with `name` in `I`, `run.provenance`, or `<slot>.<port>` whose slot is not *never* and whose port's `when` holds. A source is *usable* when it is an input, `run.provenance`, or a port of a slot that can run in this deployment (its `when` holds, it has a usable implementation, and each of its required ports has a qualifying usable source, recursively); it is *preferred* when it is an input, `run.provenance`, a slot already planned as `target` or `upstream`, or a non-optional slot that runs *definitely*. The first qualifying source that is usable and preferred is picked; else the first usable one; else the first preferred one; else the first qualifying one (the last three can make an optional slot required for this run). A picked slot not yet planned gets role `upstream` and joins the work list. No qualifying source refuses the run with `target_unreachable`, naming the target, the slot, the port and the chain of slots that needed it. Optional consumed ports never pull slots in.
4. **Close the best-effort part.** The same closure from `T_opt`, with role `if_available` for slots not already planned, and slots planned with any role counted as preferred. Failures here do not refuse the run.
5. **Statuses**, in dependency order, by the rules of pipeline.md §6 items 1-4 applied to the planned slots: a slot whose `when` is false is `skipped` with reason `condition_false` (also when it was not requested, so that the full plan and a preset closing to the same slots give the same statuses); a slot outside the closure is `skipped` with reason `not_requested`; a planned slot with a required port none of whose qualifying sources is an input, `run.provenance` or a port of a `planned` slot is `skipped` with `upstream_absent`; a slot without a usable implementation is `unavailable` with `no_implementation`; every other slot is `planned`. A `target` or `upstream` slot that is not `planned` refuses the run: with `no_implementation` (naming the target and the slot that needed it, `required_by`) when it has no usable implementation, otherwise through the reason already recorded for the slot it depends on.

*Rationale for the order of preference:* sources that can run keep a required slot from being refused while another listed source would serve (as in the run resolution, which accepts any available source); among them, planned and definite sources keep a required slot from depending on a best-effort one. The engine still stages the first *available* source at run time (pipeline.md §3.1), so an earlier source that is in the plan as `if_available` keeps precedence when it runs.

For a pipeline that passes the static availability check (pipeline.md §3.3), `target_unreachable` cannot occur, because every slot that can run has a source for each required port; the closure still reports it, so that it is safe on any pipeline document.

Every feasible plan gives a plan record ([provenance.md §3.4](provenance.md#34-plan-record)) that the 1.2 rules accept: `targets` is `{}` for R0; otherwise it lists the ports of every required target, never a slot both required and if available, and at least one target.

### 4.1 Plan roles

| Role | Meaning |
|---|---|
| `target` | Named by a required target (in R0: a non-optional applicable slot). |
| `upstream` | Needed by a `target` or `upstream` slot. |
| `if_available` | Named by an if-available target, or needed only by such a slot (in R0: an optional applicable slot not needed by a non-optional one). |

Slots with reason `condition_false` or `not_requested` have no role.

### 4.2 Refusal reasons

Each reason is `{code, message, target, slot, port, required_by}` (fields present when relevant). Messages are English and name no person, host or file.

| Code | When |
|---|---|
| `unknown_target` | Step 2: the target names no slot, produced port or produced artefact type. |
| `ambiguous_target` | Step 2: several candidate ports, or a name that is both a slot and an artefact type. |
| `target_not_applicable` | Step 2: a required target whose slot or port does not run under the input set. |
| `target_not_guaranteed` | Step 2: a required target on a port with `optional: true`, or on a slot without a guaranteed port. |
| `conflicting_targets` | Step 2: a slot is both a required and an if-available target. |
| `empty_request` | Step 2: targets were given but none applies. |
| `target_unreachable` | Step 3: a required port of a planned slot has no qualifying source. |
| `no_implementation` | Step 5: a `target` or `upstream` slot has no usable implementation. |

The API adds request-level codes (`free_mode_not_allowed`, `preset_not_found`, `preset_not_enabled`, `preset_input_set_mismatch`, `preset_and_targets`, `implementation_fixed_by_preset`) and HTTP statuses ([run-plans.md §3.3](../design/run-plans.md#33-refusal-reasons)); they are not part of this contract.

### 4.3 Notes for the implementing cards

- **F1-19 and F1-23.** Once the orchestrator writes 1.2 run records, it MUST copy a selected step's manifest `upstream` block into `implementation.upstream` ([provenance.md §3.1](provenance.md#31-slot-records)); the validator rejects a 1.2 record that omits it. This copy has to exist before any step manifest of an enabled implementation gains an `upstream` block (F1-23 adds the first one, to `flnm-fast`).
- **F1-19.** The orchestrator passes as `enabled`, per slot, the implementations its choice rule can select (pipeline.md §6 item 3), so that R0 keeps refusing and skipping exactly as the run resolution does.
- **F1-19.** Step images copy the contract schemas when they are built; the report step validates its `run_provenance` input against that copy. Its image must be rebuilt with the 1.2 schemas before the orchestrator writes `plan`, `plan_role`, `not_requested` or `origin`.

## 5. Implementation choice with presets

The rule of [pipeline.md §6](pipeline.md#6-run-resolution) item 3 gains two cases for runs created from a preset: a `fixed` choice wins and is recorded as `chosen_by: preset` (a different user choice refuses the run); a `default` comes after the user's choice and before the deployment default, and is also recorded as `chosen_by: preset`. Enablement still applies: a preset never enables an implementation.

## 6. Versions

| Bump | When |
|---|---|
| MAJOR | A required target removed or changed; a fixed implementation added; an input set removed. |
| MINOR | An if-available target, a default implementation or an accepted input set added; the pipeline range widened. |
| PATCH | Title and description only. |

Disabling a preset in a deployment only hides it for new runs; runs keep their frozen plan and can be resumed.
