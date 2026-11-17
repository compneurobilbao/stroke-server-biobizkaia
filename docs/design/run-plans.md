# Run plans: presets, free mode and external tools per slot

Status: **approved design** (card DES-01; approved by the project owner, 16 Nov 2026). Decision record: [ADR-0018](../adr/0018-presets-free-mode-external-tools.md) (accepted). The owner's decisions on the open points are in [section 7](#7-owner-decisions-16-nov-2026). Nothing in this document is implemented yet; it becomes normative through the contract, data-model and pipeline changes of the cards in [section 6](#6-phased-implementation), which are also in the implementation plan (section 5.9).

Builds on: [pipeline.md](../contracts/pipeline.md) (slots, sources, conditions, static availability check, run resolution), [ADR-0015](../adr/0015-selectable-implementations-per-slot.md) (D15), [data-model.md](../data-model.md) §4.3, §4.5, §4.11, §7, §8, §9, [api.md](../api.md) (`/api/v1`), `orchestrator/resolution.py`, `orchestrator/engine.py`, `pipelines/stroke-lnm/`, `frontend/src/features/cases/`.

## 1. Problem and terms

Today a run executes a whole pipeline: `create_run` writes one `run_slots` row per slot, every applicable slot is attempted, optional slots run when they have an enabled implementation, and the user chooses one implementation per slot. The requests this design answers:

1. **Presets**: named, versioned analyses ("full lesion network mapping", "CT normalisation only", "lesion QC and report", "MNI-mask analysis") that a user picks by name.
2. **Free mode**: the user says which outputs or steps they want; the platform builds a valid plan, adds what is needed upstream, refuses impossible plans with clear reasons, and freezes the plan in the run so that provenance and resume work as for presets.
3. **External tools per slot** (for example third-party lesion segmenters), and **results produced outside the platform** (for example a mask segmented elsewhere) entering a run as inputs.

Terms used below:

| Term | Meaning |
|---|---|
| Plan request | What the user (or a preset) asks for: a set of targets, implementation choices, and the run inputs (which determine the input set). |
| Target | A slot (`tract-disconnection`), a produced port (`normalisation.lesion_mask_mni`) or an artefact type (`tract_disconnection`). Each target is **required** or **if available**. |
| Closure | The set of slots needed to produce the targets under the run's input set, following the pipeline's wiring. |
| Plan role | Why a slot is in the plan: `target` (asked for, required), `upstream` (needed by a required slot), `if_available` (asked for as best effort, or needed only by such a slot). Slots outside the closure are `skipped` with reason `not_requested`. |
| Full plan | The plan request implied when neither a preset nor targets are given: today's behaviour ([3.2](#32-resolution-algorithm), rule R0). |
| Run-plan policy | Deployment setting that says who may use free mode ([3.5](#35-permissions-and-deployment-policy)). |

## 2. Presets

### 2.1 What a preset is

A preset is a reviewed, versioned **plan request** over one pipeline. It does not define slots, wiring or images; it selects inside a pipeline. One file per preset, next to the pipeline it refers to:

```
pipelines/stroke-lnm/
  pipeline.yaml
  registry.yaml
  presets/
    full.yaml
    mni-mask-analysis.yaml
    ct-normalisation.yaml
    lesion-qc-report.yaml
```

```yaml
contract_version: "1.2"
kind: Preset
id: mni-mask-analysis              # kebab-case, unique per pipeline id
version: 1.0.0                     # semantic version of this preset
title: Disconnection analysis of an MNI lesion mask
description: >-
  Functional and tract-wise disconnection, the research report and, where
  available, the COGNET-STROKE profile, for a lesion mask already in MNI space.
pipeline:
  id: stroke-lnm
  versions: ">=0.1.0 <0.2.0"       # range of pipeline versions this preset was written for
input_sets: [mni-mask]             # MUST be a non-empty subset of the pipeline's input sets
targets:
  required: [tract-disconnection, functional-disconnection, report]
  if_available: [cognet-profile]
implementations:                   # optional, per slot
  functional-disconnection: {default: flnm-fast}   # preset default; the user may change it
  # report: {fixed: research-report}               # fixed: the user may not change it
```

*Rationale for one file per preset, outside `pipeline.yaml`:* adding or changing a preset must not change the pipeline's SHA-256 and version (runs and resume are bound to them, data-model.md §7.3), and a deployment must be able to enable presets independently of implementations.

### 2.2 Validation rules

Checked by `contracts/validate.py` (single document and against a pipeline) and by the orchestrator when it loads presets; an invalid preset is logged and not offered, never half-loaded (as for pipelines, `orchestrator/catalog.py`).

| Rule | Check |
|---|---|
| P1 | The document validates against `contracts/schemas/v1/preset.schema.json` and declares contract 1.2 or later. |
| P2 | `pipeline.id` names a loaded pipeline; the preset is attached to every loaded version of it inside `pipeline.versions`, and validated against each of them separately (P3-P7). A version for which validation fails does not offer the preset; the others still do. |
| P3 | `input_sets` are input sets of that pipeline version. |
| P4 | Every target resolves ([3.2](#32-resolution-algorithm), step 2) under every listed input set; ambiguous or unknown targets are errors. |
| P5 | For every listed input set, the plan is feasible when every allowed implementation is assumed enabled (static check; enablement is checked again at run creation). |
| P6 | `implementations` keys are slots of the plan for at least one listed input set; values name allowed implementations; a slot has `default` or `fixed`, not both. A fixed stub is an error. |
| P7 | A slot both in `required` and `if_available` is an error. |
| P8 | `(id, version)` is unique per pipeline id; a changed file with the same version is refused at load (the SHA-256 recorded in runs would otherwise stop matching what the version means). |

### 2.3 Listing and choosing

- `GET /api/v1/pipelines/{id}/versions/{version}/presets` lists the presets attached to that pipeline version that are **enabled** in the deployment, with title, description, accepted input sets, the plan per input set (slots and roles, from the static resolution), and `available: false` plus the reasons when the deployment currently cannot run it (for example a required slot has no enabled implementation).
- The wizard filters presets by the input set of the mode the user chose and preselects the deployment default preset.
- A run request names a preset by id, and optionally version (default: the highest enabled version attached to the chosen pipeline version).

### 2.4 Governance

| Question | Rule |
|---|---|
| Who adds a preset | Maintainers, by pull request to the repository that holds the pipeline. Same review as a pipeline change; no Opus review needed unless the PR also changes `contracts/`. Presets are not created at run time or through the API. |
| Who enables it | Deployment configuration (`registry.yaml`, `profiles.<profile>.presets: {enabled: [...], default: <id>}`), seeded into a `preset_settings` table, then administrators through admin endpoints (as `slot_implementation_settings`, data-model.md §4.11). |
| Version bumps | Patch: text only. Minor: adds an if-available target, a default implementation, an accepted input set, or widens the pipeline range. Major: removes or changes a required target, adds a fixed implementation, removes an input set. |
| Withdrawal | Disabling a preset only hides it for new runs. Runs already created keep their frozen plan and can be resumed (the plan lives in `run_slots`, not in the preset). |
| Default preset | At most one per pipeline id per deployment. If the deployment has no enabled preset, the wizard offers the full plan (and free mode, if the policy allows). |

### 2.5 Presets for `stroke-lnm` 0.1.0 (worked examples)

| Preset | Input sets | Required | If available | Resulting plan |
|---|---|---|---|---|
| `full` | both | `tract-disconnection`, `report` | `functional-disconnection`, `cognet-profile` | MNI mask: `lesion-intake` (upstream), `tract-disconnection`, `report`; functional and COGNET if enabled. CT: as before plus `normalisation` (upstream, because `lesion_mask` takes the first available source). Same slots and statuses as the full plan (only `plan_role` differs: slots reached from the targets are `upstream`); a test asserts the equality for every input set. `tract-disconnection` is named because from `stroke-lnm` 0.2.0 (F1-26) the report's tract table is optional, and `report` alone would no longer pull it in (contract tests of F1-17). |
| `mni-mask-analysis` | `mni-mask` | `tract-disconnection`, `functional-disconnection`, `report` | `cognet-profile` | `lesion-intake` (upstream, provides `lesion_mask_mni` and `lesion_qc`), the three targets, COGNET if enabled. Unlike `full`, functional disconnection is required: the run is refused when it has no enabled implementation instead of silently running without it. |
| `ct-normalisation` | `ct-and-native-mask` | `lesion-intake`, `normalisation` | — | Intake and normalisation only. Valid as a document today; offered as `available: false` (`no_implementation`, slot `normalisation`) until the normalisation step is enabled. |
| `lesion-qc-report` | both | `lesion-intake`, `report` | — | In 0.1.0 the report slot requires `tract_disconnection`, so the plan would add `tract-disconnection` (role `upstream`, and in CT mode `normalisation`). Owner decision 5: the report must accept runs without tract disconnection. Card F1-26 makes that port optional in a `stroke-lnm` minor version (0.2.0) and the report step handles its absence; with it the plan is `lesion-intake` and `report` only (plus `normalisation` in CT mode, because the report needs `lesion_mask_mni`). The preset's pipeline range starts at that version. |

## 3. Free mode

### 3.1 Approaches compared

| | A. Target-driven plans inside a reviewed pipeline (**recommended**) | B. Ad-hoc plans composed from a slot catalogue | C. One pipeline file per preset |
|---|---|---|---|
| Idea | The pipeline stays the reviewed superset graph. A plan request selects targets; the resolver closes them over the existing `from` wiring; everything else is `not_requested`. Presets and free mode are the same mechanism. | Slots are defined once in a global catalogue with typed ports. The planner wires slots by artefact type and synthesises a pipeline document per run. | Each analysis is its own `pipeline.yaml`; "free mode" is impossible or means editing YAML. |
| Wiring | Reviewed `from` lists, including fallbacks (`[normalisation.lesion_mask_mni, lesion-intake.lesion_mask_mni]`). | Inferred from types. Ambiguous whenever two slots produce one type (`lesion_mask_mni`, `qc_figure`); needs tie-break rules, which is exactly the type-only matching ADR-0015 rejected. | Reviewed, but duplicated per file. |
| Validation | Static availability check at load (unchanged) plus a cheap closure per run. | Full wiring and availability check per run; no load-time guarantee. | At load. |
| Reproducibility | Pipeline SHA-256 + recorded plan request + frozen `run_slots`. | Needs the synthesised document stored per run (new table or file) and its SHA-256. | Pipeline SHA-256. |
| Engine, resume, case store | Unchanged: `not_requested` is a `skipped` status, the engine never schedules skipped slots. | Engine must load per-run definitions instead of the reviewed file by SHA-256; `pipeline_unavailable` semantics change. | Unchanged. |
| Migration | One additive revision (section [3.7](#37-data-model-mapping)). | Additive plus a table for per-run definitions and changes to the engine's pipeline lookup. | None, but every preset multiplies implementation lists and registry entries. |
| Flexibility | Anything the pipeline wires. Cross-pipeline combinations need a pipeline version that contains the slots. | Any combination of catalogued slots, including across pipelines. | None at run time. |
| Review point (ADR-0015) | Kept: only reviewed wiring and images run. | Weakened: every run is a new, unreviewed graph. | Kept. |

A variant of A, *"optional slots in a superset pipeline"* (users switch off optional slots), was also considered: it cannot leave out non-optional slots such as `report`, cannot make an optional slot required, and asks users to think in slots to remove rather than outputs they want. Target-driven planning subsumes it (the full plan is that variant with nothing switched off).

**Recommendation: A.** It answers every example the owner gave, keeps one reviewed graph per pipeline, needs no engine change, and keeps a path to B open: if cross-pipeline composition is needed later, a synthesised pipeline can be stored and referenced through the same plan columns.

### 3.2 Resolution algorithm

Inputs: pipeline `P` (one loaded version), the run's input files (their roles give the input set `I`, pipeline.md §2, unchanged), the plan request (targets `T_req`, `T_opt`; implementation choices; preset defaults or fixed choices), the deployment's enablement.

- **R0 (full plan).** No preset and no targets: `T_req` = every non-optional slot whose `when` holds under `I`; `T_opt` = every optional slot whose `when` holds. This reproduces the current `create_run` exactly; a property test compares both on every pipeline and input set in the repository.

1. **Static availability.** Evaluate pipeline.md §3.3 for `I`: per slot *definitely / possibly / never*, per produced port the same, using the port's `when`.
2. **Resolve targets** to `(slot, ports)`:
   - a slot name: the slot and its *guaranteed* ports (no `optional: true`; `when`, if any, holds under `I`);
   - `slot.port`: that port;
   - an artefact type: the produced ports of that type whose slot is not *never* under `I` and whose `when` holds. Exactly one candidate is required, else `ambiguous_target` (listing candidates as `slot.port`) or `unknown_target`.
   - A required target whose slot is *never* under `I` (condition false) is refused with `target_not_applicable`. A required target on a port that is not guaranteed (`optional: true`) is refused with `target_not_guaranteed`; the same target as if-available is accepted.
3. **Close.** Work list starting with the required target slots (role `target`). For each planned slot and each consumed port without `optional: true`, take the sources of its `from` list in order. A source *qualifies* under `I` when it is `inputs.<name>` with `name` a role of `I`, `run.provenance`, or `<slot>.<port>` whose slot is not *never* and whose port's `when` holds. Pick, in list order, the first qualifying source that is an input, `run.provenance`, a slot already planned as `target` or `upstream`, or a non-optional slot that runs *definitely* under `I`; only if there is none, the first qualifying source at all (which makes an optional slot required for this run). Earlier sources that end up in the plan as `if_available` keep precedence at run time, because the engine stages the first available source (pipeline.md §3.1), so the fallback behaviour of today's runs is preserved. Add the picked slot with role `upstream` (unless already `target`). If no source qualifies, refuse with `target_unreachable`, naming the target, the chain of slots and the port. Optional consumed ports never pull slots in: they are staged only if their producer is in the plan for another reason (the preview says so, [3.6](#36-ui)).
4. **Close the best-effort part.** Same closure from `T_opt`, role `if_available` for slots not already planned. A failure here does not refuse the run: the slot and the slots that need it become `skipped` (`upstream_absent`) or `unavailable` (`no_implementation`), as optional slots behave today.
5. **Everything else** gets `status = skipped`, `reason = not_requested`.
6. **Implementations**, for planned slots in dependency order: the existing rule (pipeline.md §6 item 3) with one addition: a preset's `fixed` choice wins and is recorded as `chosen_by = preset`; a preset's `default` comes after the user's choice and before the deployment default (`chosen_by = preset`). A user choice that differs from a `fixed` one is refused (`implementation_fixed_by_preset`). A `target` or `upstream` slot without an enabled implementation refuses the run with `no_implementation`, now also naming the target that needed it (`required_by`).
7. **Freeze.** Effective parameters and bundles as today; one `run_slots` row per pipeline slot with `plan_role`; the request and resolved targets on `pipeline_runs`.

Every refusal collects **all** reasons found (not only the first), so that the UI can show them together.

### 3.3 Refusal reasons

Response body: the existing `{detail, code, slot}` plus `reasons[]`, each `{code, target, slot, port, required_by, message}` (fields present when relevant). `code` of the response is the first reason's code.

| Code | HTTP | When |
|---|---|---|
| `free_mode_not_allowed` | 403 | Targets given but the run-plan policy does not allow free mode for this user. |
| `preset_not_found` | 404 | Unknown preset id or version for this pipeline version. |
| `preset_not_enabled`, `preset_input_set_mismatch`, `preset_and_targets` | 422 | Disabled preset; the run's input set is not accepted by the preset; both a preset and targets given. |
| `unknown_target`, `ambiguous_target`, `target_not_applicable`, `target_not_guaranteed`, `target_unreachable` | 422 | Step 2 and 3 above. |
| `implementation_fixed_by_preset` | 422 | A user choice contradicts a fixed preset choice. |
| `no_implementation` | 409 | Unchanged code; the body adds `required_by`. |

Example messages (English, no identifiers): "The target 'normalisation' does not run with the input set 'mni-mask' (it needs the inputs ct and lesion_native)." "The slot 'cognet-profile' needs 'fdisconnection_map' from 'functional-disconnection', which has no enabled implementation in this deployment."

### 3.4 Reproducibility and provenance

- The run records `plan_mode` (`full`, `preset`, `free`), the preset id, version and SHA-256 (exact bytes) when a preset was used, and the plan request as resolved (required and if-available targets as `slot.port` lists). Together with the pipeline SHA-256 (already recorded) and the frozen `run_slots`, this is enough to re-submit the same request ("run again with the same plan") and to explain every slot's presence or absence.
- `RunProvenance` gains `plan {mode, preset {id, version, sha256} | null, targets {required[], if_available[]}}`; slot records gain reason `not_requested`, `chosen_by: preset`, and `plan_role`. The report step receives this through `run.provenance` and states the plan ("Preset mni-mask-analysis 1.0.0" or "Custom plan: targets ...").
- **Resume and retry** are unchanged: they act on `selected` slots without a succeeded attempt; `not_requested` slots never get attempts. The resume preconditions (data-model.md §7.3) are unchanged; in particular, disabling a preset does not block resume, while disabling an implementation still does.
- Seeds, effective parameters and bundles are frozen per run exactly as today.

### 3.5 Permissions and deployment policy

Deployment setting `RUN_PLAN_POLICY` (environment, read once at start like `DEPLOYMENT_PROFILE`):

| Value | Presets and full plan | Free mode |
|---|---|---|
| `presets_only` | everyone allowed to create runs | nobody |
| `free_for_admins` | everyone allowed to create runs | `admin` |
| `free_for_researchers` | everyone allowed to create runs | `admin`, `researcher` |

Values per profile (D14, owner decision 1): `research-public` and `mask-only`: `presets_only`, fixed (external users get reviewed analyses only; free mode adds support burden and makes results harder to compare across users); a configuration that sets another value on these profiles is refused at start. `on-premise`: `free_for_researchers` by default; the lab instance runs with it as soon as free mode is released (Phase B). Development and CI: `free_for_researchers`.

Permission matrix additions (data-model.md §9): "Create run with a preset or the full plan" = the existing "Create run" row; new row "Create run in free mode (targets)" = admin `all` and researcher `own` only when the policy allows, otherwise `no`; "Enable or disable presets, set the default preset" = admin `all`. Free mode never widens what a user can run: implementations still have to be enabled, and the plan only contains slots of a loaded, reviewed pipeline.

### 3.6 UI

Changes to the case wizard (`frontend/src/features/cases/`), whose current steps are mode, files and implementation choices:

1. **Mode** (unchanged): the input set, which decides the files to upload.
2. **Analysis** (new): cards for the enabled presets that accept the mode, the default preselected, unavailable presets shown disabled with their reason. If the policy allows free mode for the user, a "Custom" card opens an output checklist grouped by slot (title, artefact types it produces, "optional enrichment" for if-available).
3. **Plan preview** (new, live): on every change the wizard calls the preview endpoint ([3.8](#38-api)) and shows each slot as *requested*, *added (needed by X)*, *if available*, *not part of this plan* or *unavailable*, with refusal reasons inline and the start button disabled while the plan is infeasible. Optional inputs of a planned slot whose producer is not planned are mentioned ("The report will not include the COGNET profile; add it to include it").
4. **Implementations** (existing `SlotSelector`): only for planned slots; slots with a fixed preset choice are shown read-only. External implementations carry an "External tool" badge with name, version and licence ([4.6](#46-labelling-in-ui-provenance-and-reports)).

Run page: the timeline lists planned slots; `not_requested` slots are collapsed under "Not part of this plan"; the header names the preset (or "Custom plan") and offers "Run again with the same plan" (pre-fills the wizard with the recorded request).

### 3.7 Data model mapping

One additive Alembic revision; no table is rewritten.

| Table | Change | Notes |
|---|---|---|
| `pipeline_runs` | `plan_mode varchar(8) NOT NULL DEFAULT 'full'`, `CHECK IN ('full','preset','free')` | Existing rows are `full`. Legacy runs keep `full`. |
| | `preset_id` kebab N, `preset_version` semver N, `preset_sha256` sha256hex N | `CHECK ((plan_mode = 'preset') = (preset_id IS NOT NULL))` and the three columns all null or all set. |
| | `targets jsonb NOT NULL DEFAULT '{}'` | Resolved request `{required: [slot.port], if_available: [slot.port]}`; `{}` for `full` (the full plan is defined by R0). `requested_implementations` is unchanged. |
| `run_slots` | `reason` CHECK adds `not_requested`; `chosen_by` CHECK adds `preset` | `ck_run_slots_selected` is unchanged: `not_requested` rows are `skipped` with a reason and no implementation. |
| | `plan_role varchar(12) N`, `CHECK IN ('target','upstream','if_available')` | Null for `not_requested`, `condition_false` and legacy rows. Back-fill of existing pipeline runs: non-optional applicable slots `target`, optional slots `if_available`. |
| | `upstream jsonb N` | Copy of the manifest's `upstream` block for selected external implementations ([4.2](#42-manifest-the-upstream-block)), so that provenance can be regenerated from the database (data-model.md §8). |
| `preset_settings` (new) | `id`, `pipeline_id`, `preset_id`, `enabled`, `is_default`, `updated_at`, `updated_by_user_id` | Same shape and rules as `slot_implementation_settings` (§4.11): keyed by pipeline id and preset id, not version; unique default per pipeline id; seeded from the registry profile. |
| `input_files` | `origin jsonb N` | Declared origin of a supplied input ([4.7](#47-user-supplied-results-as-run-inputs)). |

Downgrade drops the new columns and table and restores the old `CHECK` lists; it refuses (with a message) when rows use `not_requested`, `chosen_by = 'preset'` or `plan_mode <> 'full'`, rather than deleting runs.

The `RunProvenance` regeneration table of data-model.md §8 gains: `plan` from `plan_mode`, `preset_*`, `targets`; `slots[].plan_role` and `slots[].implementation.upstream` from `run_slots`; `inputs[].origin` from `input_files.origin`.

### 3.8 API

All additive to `/api/v1`; existing clients that send neither `preset` nor `targets` get the full plan, as today.

| Endpoint | Change |
|---|---|
| `GET /pipelines/{id}/versions/{version}/presets` | New. Enabled presets attached to this version, with static plans per input set and availability. |
| `POST /pipelines/{id}/versions/{version}/plan` | New, dry run, no writes. Body: `input_roles`, `preset {id, version?}` or `targets {required[], if_available[]}`, `implementations`. Returns `{feasible, input_set, slots[{name, status, reason, plan_role, required_by, implementation}], reasons[]}`. Same policy checks as run creation (403 for free mode when not allowed). |
| `POST /cases/{id}/runs` | Adds optional `preset {id, version?}` and `targets {required[], if_available[]}` (mutually exclusive). Errors of [3.3](#33-refusal-reasons). |
| `GET /runs/{id}` | Adds `plan {mode, preset, targets}` and per slot `plan_role` and `upstream`. |
| `GET /admin/presets`, `PUT /admin/presets/{pipeline_id}/{preset_id}` | New, administrators: `{"enabled": bool, "is_default": bool}`, same semantics as the implementation endpoints. |
| `PUT /cases/{id}/inputs/{role}` | Adds an optional `origin` form field ([4.7](#47-user-supplied-results-as-run-inputs)). |
| `GET /config` (or the existing pipelines listing) | Exposes the caller's effective right to use free mode, so that the UI does not offer it in vain. |

The OpenAPI snapshot (`docs/api/openapi-v1.json`) is regenerated by the implementing card.

### 3.9 What does not change

- `pipeline.yaml` format and its validator rules (contract 1.1 pipelines stay valid; presets are separate documents).
- The engine (`orchestrator/engine.py`): readiness, source resolution (first available source of a `from` list; skipped upstream slots are passed over), completion ("every selected slot succeeded"), cancel, resume, retries and crash recovery.
- The case store layout and the rule that a step version fills one slot per pipeline.
- One active run per case (`uq_pipeline_runs_active_case`). Free mode makes incremental work more likely (normalise now, analyse later); reusing artefacts of an earlier run is a later feature, out of scope for the first free-mode release (owner decision 6; [4.7](#47-user-supplied-results-as-run-inputs), origin `platform-run`).

### 3.10 Contract 1.2 (minor)

| Document | Change |
|---|---|
| New `Preset` document | Schema `contracts/schemas/v1/preset.schema.json`, normative text `docs/contracts/presets.md`, worked example, rules P1-P8 in `contracts/validate.py`. |
| `provenance.json` (`RunProvenance`) | Optional `plan`; slot reason `not_requested`; `chosen_by: preset`; optional `plan_role` and `implementation.upstream` in slot records; optional `origin` on `inputs[]`. |
| `step.yaml` | Optional `upstream` block ([4.2](#42-manifest-the-upstream-block)). |
| `pipeline.yaml`, `bundle.yaml`, `error.json`, artefact types | Unchanged. |

All changes are additive; documents declaring 1.0 or 1.1 stay valid; records using the new values declare 1.2 (versioning.md §2).

## 4. External tools per slot

### 4.1 The wrapper step pattern

External tools enter only as steps (D1, ADR-0015); `flnm-fast` is the existing example (`steps/flnm-fast/`, a wrapper over a public engine image pinned by digest in its `Dockerfile`). For a third-party segmenter (a future `segmentation` slot consuming `ct_native` and producing `lesion_mask_native`):

1. **Image.** The wrapper's `Dockerfile` starts `FROM <upstream image>@sha256:<digest>` (or copies the tool from it in a multi-stage build), adds the step SDK and an entry point implementing `run --in /in --out /out --params /in/params.json`. The upstream digest is pinned in the `Dockerfile`; the wrapper image is pinned by digest in `pipeline.yaml` as every implementation.
2. **Adapter only.** The wrapper converts formats and names (staging `/in/ct.nii.gz` to the tool's expected layout; writing `/out/lesion_mask_native.nii.gz` with the artefact type's constraints). Any step that changes results (thresholding a probability map, resampling) is an explicit parameter with its default, recorded in provenance; the tool's algorithm is not modified (AGENTS.md).
3. **No network.** Steps run without network (D1). Tools that download weights on first use are configured to read them from `/ref/<name>/`; the wrapper's reference test runs with networking disabled to prove it.
4. **Weights as bundles.** Model weights are a bundle of type `segmentation-model` (ADR-0006, D10): `bundle.yaml` with licence, `redistributable`, citation, and metadata (modality, training population summary, version of the upstream release). A step that needs a model declares the bundle requirement; the implementation entry may pin the bundle version.
5. **Configured variants.** Several models of one tool are several implementation entries of the same image with different bundle pins or parameters (pipeline.md §4 item 2).
6. **Repository.** The wrapper lives in `steps/<name>/` of this repository or in a lab repository pinned by commit in `registry.yaml` (as `lnm-tracts` and `cognet-stroke`).

### 4.2 Manifest: the `upstream` block

Optional in contract 1.2, required by the registry check for implementations whose registry entry has `origin: external`:

```yaml
upstream:
  name: example-segmenter            # upstream project name
  version: 2.1.0                     # upstream release wrapped by this step version
  homepage: https://example.org/example-segmenter
  image: docker.io/example/segmenter@sha256:<64 hex>   # the base image, as in the Dockerfile
  licence: Apache-2.0                # SPDX of the upstream code
  citation: "..."                    # optional; used by the report
```

The upstream tool also appears in `licence.third_party[]` (step-contract.md §1) and at run time in `StepProvenance.software[]`. The orchestrator copies the block into `run_slots.upstream` at run creation.

### 4.3 Licence and data-use checks

Recorded in `registry.yaml` per implementation (`origin: external`, `licence_review: {code, weights, image_redistribution, notes, reviewed: <date>}`) and checked by `registry.py check`:

| Check | Rule |
|---|---|
| Upstream code licence | SPDX known; permits use for research and redistribution of the wrapper image on the public registry (D7). If redistribution is not allowed, see *Local builds* below. |
| Weights licence | From the bundle's `licence`; non-commercial licences are acceptable for research deployments and must be shown in the UI; `redistributable: false` keeps the bundle out of on-premise tarballs (bundles.md §4). |
| Training data terms | Recorded in the bundle's model card or notes; weights trained under a data use agreement that forbids distribution of derived models are not accepted. |
| Intended use | Every result stays "research use only"; a tool whose licence forbids that use (or requires registration per user) is not accepted. |
| Digest pinning | Upstream image and wrapper image pinned by digest; `registry.py check` fails on tags. |

**Local builds (owner decision 7, a minimal exception to D7).** When the upstream licence forbids publishing an image that contains the tool, the wrapper's source (Dockerfile, adapter code, manifest) stays public in the repository, but its image is never pushed to the public registry. The registry entry is marked `build: local`; each deployment builds the image itself from the pinned source and the upstream artefact it obtained under the licence, and enables the implementation with the digest of its own build (the deployment's materialised pipeline, as `registry.py materialize` does in CI). Rules: the upstream artefact is pinned by digest or checksum, so that every local build of one step version is reproducible and the digest recorded in provenance identifies it; CI builds the image to run the reference test but does not publish it; on-premise installers do not include it (as non-redistributable bundles, bundles.md §4); the UI and report label such implementations like any other external tool. Nothing else in D7 changes: platform code, steps' source and pipeline definitions remain public.

### 4.4 Validation before enabling

| Status (`registry.yaml`, `validation.status`) | Requires | Where it may be enabled |
|---|---|---|
| `unvalidated` | Manifest valid, slot match M1-M5 | CI and demo deployments only (like stubs). |
| `reference-tested` | The contract reference test (step-contract.md §1.7) passes in CI for this digest on synthetic fixtures; the nightly reference CI (F1-14) covers it | Deployments whose minimum level allows it; labelled "experimental" in the UI. |
| `validated` | Agreement with reference results on reference cases, run by lab staff (agents never handle real data, plan §6), with the metrics and thresholds set by the responsible scientist (for segmentation for example Dice and volume difference against reference masks), recorded in `validation.record` (a document in the repository, no data) and signed off (plan §7) | All deployments. |

Deployment configuration `MIN_IMPLEMENTATION_VALIDATION` (owner decision 8): `validated` on `research-public` and `mask-only`, and it cannot be lowered there (a lower value is refused at start); `reference-tested` on `on-premise`, which includes the lab instance, where such implementations are labelled "experimental"; `unvalidated` only in CI and demo deployments. The admin endpoint refuses to enable an implementation below the minimum with `implementation_not_validated`. The rule applies to all implementations, not only external ones, but it matters most for them because their behaviour on the project's data is unknown.

### 4.5 Comparing external and in-house results

Because every implementation of a slot produces the same artefact types, a researcher can run the same case twice with two implementations of the segmentation slot (two runs, one plan each) and compare the masks in the viewer. A dedicated comparison view is out of scope here.

### 4.6 Labelling in UI, provenance and reports

- **UI**: implementation selector and run page show an "External tool" badge with upstream name, version and licence, plus the validation status badge ("experimental" for `reference-tested`).
- **Provenance**: the slot record carries `implementation.upstream`; `software[]` names the upstream version actually run; the model bundle appears with its content digest.
- **Report**: a section "Tools used" lists, per slot, the implementation, step version, image digest, and for external tools the upstream name, version, licence and citation; results from `reference-tested` implementations carry an "experimental method" note next to the result.

### 4.7 User-supplied results as run inputs

**Mechanism (no contract change needed).** A substitution the project wants is written into the pipeline with input sets and source lists, which contract 1.1 already supports. For a future segmentation slot:

```yaml
input_sets:
  - {name: ct-only, inputs: [ct]}                          # the platform segments
  - {name: ct-and-native-mask, inputs: [ct, lesion_native]} # the user supplies the mask
slots:
  - name: segmentation
    when: {inputs_absent: [lesion_native]}
    consumes: [{port: ct, type: ct_native, from: inputs.ct}]
    produces: [{port: lesion_mask_native, type: lesion_mask_native}]
  - name: normalisation
    consumes:
      - {port: ct, type: ct_native, from: inputs.ct}
      - {port: lesion_mask, type: lesion_mask_native,
         from: [segmentation.lesion_mask_native, inputs.lesion_native]}
```

The supplied mask goes through the same intake checks (type checks at upload and in the SDK; the `lesion-intake` slot's QC) as one produced by a step.

**Declared origin.** At upload the user may declare where the file comes from: `origin: {kind: manual}` (drawn by hand), `{kind: external-tool, tool: <name>, version: <version>}`, later `{kind: platform-run, run_id, artifact_id}`. Tool name and version are restricted to `[A-Za-z0-9._+-]{1,64}` (no free text, which could carry identifiers). The platform cannot verify a declared origin; UI and report say "declared by the user". The origin is stored in `input_files.origin` and copied into `RunProvenance.inputs[].origin`.

**Reusing an artefact of an earlier run** (`platform-run`): useful with free mode (normalise in one run, analyse in another). It needs the artefact to be registered as an input file of the case (by reference, keeping its SHA-256 and the producing run), and the input set to accept it. Out of scope for the first free-mode release (owner decision 6); a later card.

**Not proposed:** replacing an arbitrary step output with an upload at run time ("override any port"). It bypasses the reviewed wiring and the slot's checks; the input-set pattern covers the cases known today. Results computed by a remote service keep the D8 path (`executor: external`, service account), which is for SP2 and not for user uploads.

## 5. Risks

| Risk | Mitigation |
|---|---|
| Free mode produces many small, incomparable runs on public instances | `presets_only` by default on public profiles; presets carry the comparable analyses. |
| A preset silently changes meaning across pipeline versions | Version range in the preset; validation per attached version (P2); preset SHA-256 recorded; major bump rule for target changes. |
| Users misread "if available" results as complete analyses | Plan preview and run page show which slots ran, were skipped and why; the report states the plan. |
| External tool licences change between releases | Licence review recorded per implementation entry and per bundle version; a new upstream version is a new step version and a new review. |
| Declared origins are trusted too much | Labelled "declared by the user" everywhere; intake QC runs on supplied masks as on produced ones. |

## 6. Phased implementation

Every card follows the common rules of plan section 5 and the review protocol of section 7. Prerequisite for all: approval of ADR-0018.

IDs are final and the cards are also in the implementation plan, section 5.9.

**Phase A: presets (no free mode exposed).** F1-26, F1-17, F1-18, F1-19, F1-20, F1-21, F1-22 with policy `presets_only` everywhere.
**Phase B: free mode.** Policy values other than `presets_only` become selectable on `on-premise` (the lab instance included); F1-22's custom card and output checklist are switched on (they ship hidden in Phase A).
**Phase C: external tools and supplied inputs.** F1-23, F1-24, F1-25; the first real external segmenter arrives with the segmentation slot (F1-06 and its pipeline version).

**F1-17 Contract 1.2: presets and plan records** · opus · Depends: ADR-0018 approved · Touches: `contracts/`, `docs/contracts/`
Add the `Preset` document (schema, normative text `presets.md`, example), rules P1-P8 in `contracts/validate.py` (single document and against a pipeline, including the static closure of [3.2](#32-resolution-algorithm) as a pure function the orchestrator can import), the 1.2 additions to the provenance schema (`plan`, `not_requested`, `chosen_by: preset`, `plan_role`, `implementation.upstream`, `inputs[].origin`) and the optional `upstream` block in `step.yaml`; version history and ADR link in `versioning.md`. Accept: examples validate; every existing 1.0 and 1.1 example still validates; validator tests for each P-rule and each refusal code of the closure; the four `stroke-lnm` presets of [2.5](#25-presets-for-stroke-lnm-010-worked-examples) as test documents give the plans in that table.

**F1-18 Data model: run plans and preset settings** · sonnet, Opus review · Depends: F1-17 · Touches: `backend/app/models/`, `backend/alembic/versions/`, `docs/data-model.md`, `tests/` (migration tests only)
The revision of [3.7](#37-data-model-mapping), back-fill of existing runs, refusing downgrade when new values are present; data-model.md sections 4.3, 4.5, new 4.12 `PresetSetting`, 8 and 9 updated. Accept: upgrade and downgrade on PostgreSQL in CI; back-fill test on a database with existing pipeline and legacy runs; constraint tests for the new `CHECK`s.

**F1-19 Orchestrator: plan resolution** · opus · Depends: F1-17, F1-18 · Touches: `orchestrator/resolution.py`, new `orchestrator/planning.py`, `orchestrator/provenance.py`, `orchestrator/catalog.py` (a `PresetRegistry` protocol and directory implementation), `tests/` (orchestrator tests)
`plan(pipeline, input_set, request, enablement) -> Plan | PlanRefused(reasons)` as a pure function; `create_run` uses it and writes `plan_mode`, preset columns, `targets`, `plan_role`, `upstream`; `RunProvenance` gains `plan`. Accept: property test that R0 equals the current resolution for every pipeline and input set in the repository; one test per refusal code; the engine runs a `not_requested` plan to `succeeded` without attempting skipped slots; resume of a preset run after the preset is disabled succeeds; provenance validates against the 1.2 schema.

**F1-20 `stroke-lnm` presets and registry profiles** · sonnet · Depends: F1-17, F1-19, F1-26 · Touches: `pipelines/stroke-lnm/`, `docs/processing-pipeline.md`
The presets `full`, `mni-mask-analysis`, `ct-normalisation`, `lesion-qc-report`; `profiles.<p>.presets` in `registry.yaml`; `registry.py check` validates presets and `enabled_settings` seeds `preset_settings`. Accept: `registry.py check` green; the `e2e-stroke-lnm` job runs `mni-mask-analysis` and asserts that `report` is `not_requested` and every other planned slot succeeded; `ct-normalisation` is listed as unavailable while normalisation is unmounted.

**F1-21 API: presets, plan preview, free-mode policy** · sonnet, Opus review · Depends: F1-18, F1-19, F1-20 · Touches: `backend/app/api/v1/`, `backend/app/schemas/`, `docs/api.md`, `docs/api/openapi-v1.json`, `tests/`
Endpoints and errors of [3.3](#33-refusal-reasons) and [3.8](#38-api); `RUN_PLAN_POLICY` with the profile defaults of [3.5](#35-permissions-and-deployment-policy); admin preset endpoints; audit event `run_created` records `plan_mode` and preset. Accept: API tests for every endpoint, refusal code and permission boundary (researcher free mode refused under `presets_only` and `free_for_admins`, allowed under `free_for_researchers`); preview and create return the same plan for the same request; OpenAPI snapshot updated.

**F1-22 Wizard and run page: analysis choice and plan preview** · sonnet · Depends: F1-21 · Touches: `frontend/src/features/cases/`, `frontend/src/api/`, `frontend/src/i18n/`
The analysis step, live plan preview, custom output checklist (hidden unless the policy allows it), read-only fixed choices, run page plan header, collapsed `not_requested` slots, "Run again with the same plan". Accept: component tests for each slot label and refusal display; Playwright test against the Compose stack with stub steps: create a run from a preset and, under `free_for_researchers`, a custom run whose preview adds an upstream slot; Spanish catalogue entries added.

**F1-23 External implementation onboarding** · sonnet, Opus review of the checklist · Depends: F1-17, F1-20 · Touches: `pipelines/stroke-lnm/registry.yaml` and `registry.py` (fields `origin`, `licence_review`, `validation`, `build: local`), `steps/flnm-fast/`, `steps/README.md` (section "Wrapping an external tool"), `backend/app/api/v1/` admin enable check (`MIN_IMPLEMENTATION_VALIDATION`, `implementation_not_validated`), `tests/`
Accept: `registry.py check` fails for an external entry without `upstream`, licence review or digest pins; admin endpoint refuses enabling below the minimum level; `flnm-fast` migrated to `origin: external` with its upstream block as the worked example (a new step version, since its manifest changes).

**F1-24 Declared origin of supplied inputs** · sonnet · Depends: F1-18, F1-21 · Touches: `backend/app/api/v1/` (input upload), `backend/app/schemas/`, `orchestrator/provenance.py`, `frontend/src/features/cases/`, `tests/`
Optional `origin` on upload with the restricted pattern, stored in `input_files.origin`, in `RunProvenance.inputs[].origin`, shown in the case and run pages. Accept: API tests for valid and refused origins (free text refused); provenance validates; UI shows "declared by the user".

**F1-25 Report: plan, external tools and declared origins** · sonnet · Depends: F1-17, F1-26 · Touches: `steps/report/`
The report states the plan (preset or custom targets), lists tools used with upstream name, version, licence and citation, marks experimental implementations and declared input origins. Accept: golden-file tests for a preset run, a custom run, and a run with an external implementation and a declared mask origin.

**F1-26 QC-only report: report slot without tract disconnection** · sonnet · Depends: none (ADR-0018 accepted) · Touches: `steps/report/`, `pipelines/stroke-lnm/`
Owner decision 5. The report step accepts a run without `tract_disconnection` (new step version; the tract section says "not part of this analysis" instead of failing) and `stroke-lnm` 0.2.0 marks the report slot's `tract_disconnection` port `optional: true` (pipeline minor version; `registry.yaml` and the materialised digests updated). Accept: report golden-file tests with and without the tract table; `contracts/validate.py` and `registry.py check` green on 0.2.0; the `e2e-stroke-lnm` job still passes with the full plan, and a run of 0.2.0 whose tract-disconnection slot has no enabled implementation succeeds with a QC-only report.

Note from F1-18 (obligation of F1-19): regenerating `RunProvenance` from the database emits `plan` and the slots' `plan_role` only when every applicable slot row of the run has a `plan_role` (data-model.md section 8); runs written by the orchestrator between migration 0013 and F1-19 have none and are regenerated without a plan.

Notes from F1-17 (contract 1.2, [presets.md §4.3](../contracts/presets.md#43-notes-for-the-implementing-cards)): once the orchestrator writes 1.2 run records (F1-19) it must copy the manifest `upstream` block into the slot record before any step manifest gains one (F1-23); `enabled` passed to the closure is the set of implementations the choice rule can select; the report image must carry the 1.2 schemas before 1.2 fields are written.

Dependency order: F1-26 can start at once; F1-17 → F1-18 → F1-19 → F1-20 (also after F1-26) → F1-21 → F1-22; F1-23 after F1-17 and F1-20 (both touch `pipelines/stroke-lnm/`); F1-25 after F1-17 and F1-26 (both touch `steps/report/`); F1-24 after F1-21. The segmentation slot with the input-set pattern of [4.7](#47-user-supplied-results-as-run-inputs) belongs to the segmentation cards (F1-06 and its pipeline version).

## 7. Owner decisions (16 Nov 2026)

| # | Question | Decision | Effect on this design |
|---|---|---|---|
| 1 | Free-mode policy per profile | `presets_only` on `research-public` and `mask-only`; free mode for researchers on `on-premise`, enabled on the lab instance from the start. | [3.5](#35-permissions-and-deployment-policy): fixed on public profiles; Phase B enables it on `on-premise`. |
| 2 | Who adds presets | Only through reviewed pull requests for now. Deployment-local presets are later work, out of scope. | [2.4](#24-governance) unchanged. |
| 3 | Saved user plans | Later, out of scope for the first release. | Not designed here. |
| 4 | First `stroke-lnm` presets | `full`, `mni-mask-analysis`, `ct-normalisation`, `lesion-qc-report`; `mni-mask-analysis` includes the report. | [2.1](#21-what-a-preset-is), [2.5](#25-presets-for-stroke-lnm-010-worked-examples). |
| 5 | QC-only report | The report step must accept runs without tract disconnection. | New card F1-26 (report step and `stroke-lnm` 0.2.0). |
| 6 | Reusing outputs of earlier runs as inputs | Later, out of scope for the first free-mode release. | [4.7](#47-user-supplied-results-as-run-inputs): `platform-run` origin deferred. |
| 7 | Upstream licence forbids publishing the image | Deployments may build the wrapper locally without publishing it: a minimal exception to D7. | [4.3](#43-licence-and-data-use-checks), *Local builds*. |
| 8 | `reference-tested` implementations | May be enabled on the lab instance (`on-premise`), labelled experimental; never on public profiles. | [4.4](#44-validation-before-enabling). |
| 9 | Contract 1.2 | Approved as described. | [3.10](#310-contract-12-minor); card F1-17. |
