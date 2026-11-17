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

- a slot name (`tract-disconnection`): the slot and its *guaranteed* produced ports (no `optional: true`, and `when`, if any, holds under the input set);
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
| P7 | A slot both in `required` and in `if_available` (as written, or after resolution) is an error. |
| P8 | `(pipeline id, id, version)` identifies one file; a second file with the same identity is refused, and a changed file with the same version is refused at load (the SHA-256 recorded in runs would otherwise stop matching what the version means). |

The validator reports a fixed implementation whose manifest is not supplied, and a preset with no supplied pipeline version in its range, as notes (errors with `--strict`), as it does for implementations of a pipeline.

## 4. Static plan closure

`plan_closure(pipeline, input_set, required, if_available, enabled)` in `contracts/validate.py` is the reference implementation: a pure function over a valid pipeline document that the orchestrator imports. It returns, per slot, the status (`planned`, `skipped`, `unavailable`), the reason and the plan role, the resolved targets, and every refusal reason found (not only the first). Choosing implementations (step 6 of the design), parameters and bundles stays with the orchestrator ([pipeline.md §6](pipeline.md#6-run-resolution)).

Inputs: pipeline `P` (one version), input set `I` (pipeline.md §2), targets `T_req` and `T_opt`, and the implementations enabled in the deployment (all allowed ones for rule P5).

- **R0 (full plan).** No preset and no targets: `T_req` is every non-optional slot whose `when` holds under `I`, `T_opt` every optional slot whose `when` holds. This reproduces the run resolution of [pipeline.md §6](pipeline.md#6-run-resolution).

1. **Static availability.** Evaluate pipeline.md §3.3 for `I`: per slot and per produced port *definitely*, *possibly* or *never*.
2. **Resolve targets** ([2](#2-fields)) to slots and `slot.port` lists. Refusals: `unknown_target` (no such slot, port, or produced type under `I`); `ambiguous_target` (an artefact type with several candidate ports, or a name that is both a slot and a produced artefact type; the message lists the candidates); for required targets only, `target_not_applicable` (the slot's or port's `when` is false under `I`) and `target_not_guaranteed` (the port has `optional: true`; the same target is accepted as if available). An if-available target that does not apply is ignored.
3. **Close.** A work list, first-in first-out, starts with the slots of the required targets in the order written (role `target`). For each slot taken from it and each consumed port without `optional: true`, the sources of its `from` list are considered in order. A source *qualifies* under `I` when it is `inputs.<name>` with `name` in `I`, `run.provenance`, or `<slot>.<port>` whose slot is not *never* and whose port's `when` holds. The first qualifying source that is an input, `run.provenance`, a slot already planned as `target` or `upstream`, or a non-optional slot that runs *definitely* is picked; only if there is none, the first qualifying source at all (which makes an optional slot required for this run). A picked slot not yet planned gets role `upstream` and joins the work list. No qualifying source refuses the run with `target_unreachable`, naming the target, the slot, the port and the chain of slots that needed it. Optional consumed ports never pull slots in.
4. **Close the best-effort part.** The same closure from `T_opt`, with role `if_available` for slots not already planned, and slots planned with any role preferred as sources. Failures here do not refuse the run.
5. **Statuses**, in dependency order: a slot whose `when` is false is `skipped` with reason `condition_false` (also when it was not requested, so that the full plan and a preset closing to the same slots give the same statuses); a slot outside the closure is `skipped` with reason `not_requested`; an `if_available` slot with a required port without a qualifying source, or whose picked producer is not planned, is `skipped` with `upstream_absent`; a slot without an enabled implementation is `unavailable` with `no_implementation`, which for a `target` or `upstream` slot also refuses the run with reason `no_implementation`, naming the target and the slot that needed it (`required_by`); every other slot is `planned`.

*Rationale for preferring planned and definite sources:* the engine stages the first *available* source at run time (pipeline.md §3.1), so an earlier source that is in the plan as `if_available` keeps precedence when it runs; preferring sources that will certainly exist keeps a required slot from depending on a best-effort one.

For a pipeline that passes the static availability check (pipeline.md §3.3), `target_unreachable` cannot occur, because every slot that can run has a source for each required port; the closure still reports it, so that it is safe on any pipeline document.

### 4.1 Plan roles

| Role | Meaning |
|---|---|
| `target` | Named by a required target (in R0: a non-optional applicable slot). |
| `upstream` | Needed by a `target` or `upstream` slot. |
| `if_available` | Named by an if-available target, or needed only by such a slot (in R0: an optional applicable slot). |

Slots with reason `condition_false` or `not_requested` have no role.

### 4.2 Refusal reasons

Each reason is `{code, message, target, slot, port, required_by}` (fields present when relevant). Messages are English and name no person, host or file.

| Code | When |
|---|---|
| `unknown_target` | Step 2: the target names no slot, produced port or produced artefact type. |
| `ambiguous_target` | Step 2: several candidate ports, or a name that is both a slot and an artefact type. |
| `target_not_applicable` | Step 2: a required target whose slot or port does not run under the input set. |
| `target_not_guaranteed` | Step 2: a required target on a port with `optional: true`. |
| `target_unreachable` | Step 3: a required port of a planned slot has no qualifying source. |
| `no_implementation` | Step 5: a `target` or `upstream` slot has no enabled implementation. |

The API adds request-level codes (`free_mode_not_allowed`, `preset_not_found`, `preset_not_enabled`, `preset_input_set_mismatch`, `preset_and_targets`, `implementation_fixed_by_preset`) and HTTP statuses ([run-plans.md §3.3](../design/run-plans.md#33-refusal-reasons)); they are not part of this contract.

## 5. Implementation choice with presets

The rule of [pipeline.md §6](pipeline.md#6-run-resolution) item 3 gains two cases for runs created from a preset: a `fixed` choice wins and is recorded as `chosen_by: preset` (a different user choice refuses the run); a `default` comes after the user's choice and before the deployment default, and is also recorded as `chosen_by: preset`. Enablement still applies: a preset never enables an implementation.

## 6. Versions

| Bump | When |
|---|---|
| MAJOR | A required target removed or changed; a fixed implementation added; an input set removed. |
| MINOR | An if-available target, a default implementation or an accepted input set added; the pipeline range widened. |
| PATCH | Title and description only. |

Disabling a preset in a deployment only hides it for new runs; runs keep their frozen plan and can be resumed.
