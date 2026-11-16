# ADR-0018: Run plans: presets, free mode and external tools per slot

- Status: accepted (approved by the project owner, 16 Nov 2026)
- Date: 2026-11-16
- Plan reference: decisions D2, D7, D15; card DES-01. Design: [docs/design/run-plans.md](../design/run-plans.md)

## Context

A run today executes one `pipeline.yaml` as a whole: every slot that applies to the run's input set is attempted, optional slots run when an implementation is enabled, and the user's only choice is one implementation per slot (D15, [pipeline.md](../contracts/pipeline.md)). Users want two more things:

1. **Pre-established pipelines** for common analyses (full lesion network mapping, CT normalisation only, lesion QC and report, analysis of an MNI mask), chosen by name, without having to know which slots matter.
2. **A free mode** where the user says which outputs or steps they want and the platform runs exactly what is needed for them.

They also want **external tools** (for example third-party lesion segmenters) to fill slots next to the lab's own steps, and results produced outside the platform (for example a mask segmented elsewhere) to enter a run as an input.

Constraints from accepted decisions: wiring is reviewed and versioned in `pipeline.yaml` (ADR-0002, ADR-0015: "the allowed list in a versioned pipeline is the review point"); a run's plan is static and known at creation, which the timeline, resume and provenance rely on (pipeline.md §3.2); steps have no network access and model weights are bundles (D1, D6); no unreviewed image ever runs (ADR-0015, rejected alternative "free choice of any registered image").

## Decision

1. **One mechanism, the run plan.** A run is created from a pipeline (unchanged) plus a *plan request*: a set of **targets**, each either *required* or *if available*. A target names a slot, a produced port (`slot.port`) or an artefact type. The orchestrator computes the plan at run creation by closing the targets over the pipeline's wiring for the run's input set: every required consumed port of a planned slot pulls in the slot that will provide it (the first source of its `from` list that can be available). Slots outside the closure get status `skipped` with the new reason `not_requested`. A required target that cannot be reached, or a slot it needs that has no enabled implementation, refuses the run with a list of reasons naming the target, the slot and the missing port. The resulting `run_slots` rows are the frozen plan; the engine, resume and retry are unchanged.
2. **The current behaviour is the "full" plan.** A request without preset and without targets means: every applicable non-optional slot is a required target and every optional slot is an *if available* target. This reproduces today's resolution exactly, so existing clients and runs keep their meaning.
3. **Presets are named, versioned plan requests, not new pipelines.** A preset is a small `Preset` document (`pipelines/<pipeline id>/presets/<preset id>.yaml`): id, semantic version, title, the pipeline id and a version range, the input sets it accepts, its required and if-available targets, and optional implementation defaults or fixed choices per slot. Presets are added only through a reviewed pull request, validated when loaded (each accepted input set must yield a feasible plan when every allowed implementation is assumed enabled), enabled per deployment like implementations (registry profile, `preset_settings` table, admin endpoints), and recorded in the run by id, version and SHA-256 of the exact bytes.
4. **Free mode is a plan request with user-chosen targets**, available only where the deployment's run-plan policy allows it (`presets_only`, `free_for_admins`, `free_for_researchers`). Public profiles (`research-public`, `mask-only`) are fixed to `presets_only`; `on-premise` (the lab instance included) allows free mode for researchers. Free mode never adds slots, wiring or images: it only selects inside a reviewed pipeline. A dry-run *plan preview* endpoint returns the plan or the refusal reasons without writing anything; the wizard calls it on every change.
5. **External tools enter as implementations of a slot**, exactly as in ADR-0015: a wrapper step that follows the contract, built on the upstream image pinned by digest, with weights as a bundle, listed in `pipeline.yaml` by digest. In addition: the manifest declares the upstream tool in an optional `upstream` block; the pipeline registry records licence review and a validation status (`unvalidated`, `reference-tested`, `validated`); a deployment refuses to enable an implementation below its minimum validation status (`validated` on public profiles; `reference-tested`, labelled experimental, on `on-premise`; stubs and `unvalidated` only in CI or demo deployments); when the upstream licence forbids publishing an image that contains the tool, deployments build the wrapper locally from its public source and it is never pushed to the public registry (a minimal exception to D7); UI, run provenance and reports label the result as produced by an external tool, with name, version and licence.
6. **User-supplied results enter as run inputs**, through input sets and source lists that the pipeline already supports (a slot skipped by `inputs_absent` when the user supplies its output; downstream `from` lists that fall back to `inputs.<name>`). The input file may carry a declared, non-verifiable `origin` (`manual`, `external-tool` with a tool name and version, later `platform-run`), recorded in provenance and shown in UI and report. Overriding an arbitrary step output with an upload is not part of this decision.
7. **Contract 1.2** (minor, additive): the `Preset` document and its schema; `RunProvenance.plan` (`mode`, preset id/version/sha256, resolved targets); slot reason `not_requested`; `chosen_by: preset`; optional `implementation.upstream` in slot records; optional `origin` on run inputs; optional `upstream` block in `step.yaml`. Documents declaring 1.0 or 1.1 stay valid.

## Consequences

- Presets and free mode share one resolver, one validation path and one provenance shape; a preset is "a saved, reviewed free-mode request". Reproducing a run means re-submitting its recorded plan request against the same pipeline SHA-256.
- No new wiring language and no per-run pipeline documents: the case store, the engine (`not_requested` slots are `skipped` and never get attempts), resume preconditions and the run provenance regeneration keep working. A preset withdrawn after a run started does not block its resume, because the plan is frozen in `run_slots`.
- Migration is one additive Alembic revision: plan columns on `pipeline_runs`, `plan_role` and `upstream` on `run_slots`, two extended `CHECK` lists, a `preset_settings` table and `input_files.origin`.
- Free mode can only compose what a pipeline already wires. A combination across pipelines needs the slots to be in one pipeline (a pipeline minor or major version), which keeps the review point where ADR-0015 put it.
- Some analyses need small pipeline changes to become plannable (for example "lesion QC and report" needs the report slot to treat the tract table as optional); the resolver states this as a refusal reason, not a silent skip.
- Labelling external tools and declared input origins makes the provenance and the report longer; licence and validation records become part of onboarding a tool.
- Implementation cards F1-17 to F1-26 are in the implementation plan, section 5.9.

## Alternatives considered

- **Ad-hoc plans composed from a global slot catalogue.** Slots defined independently, the planner wires them by artefact type and stores a synthesised pipeline document per run. Rejected for now: type-only wiring is ambiguous when several slots produce the same type (two producers of `lesion_mask_mni`, many of `qc_figure`), which ADR-0015 already rejected for step matching; every run would carry an unreviewed graph; the orchestrator would need per-run definitions instead of loading a reviewed file by SHA-256, and the static availability check would move from load time to every run. Kept as a possible later step: the plan columns proposed here would carry it.
- **One `pipeline.yaml` per preset.** Rejected: duplicated wiring and implementation lists, the combinatorial growth ADR-0015 avoided; still appropriate for genuinely different pipelines (another modality).
- **Free mode as per-run toggles of optional slots.** Rejected: users think in outputs, not in slots to switch off; non-optional slots (report, tract disconnection) could never be left out; and there is no way to say "this one is required" for an optional slot.
- **Presets inside `pipeline.yaml`.** Rejected: adding a preset would change the pipeline's SHA-256 and version, and deployments could not enable presets independently of the pipeline.
- **User-supplied images or user-uploaded results replacing any step output.** Rejected (as in ADR-0015) for images; for results, deferred: it bypasses the reviewed wiring and the intake checks, and input sets already cover the substitutions the project needs.

## Owner decisions (16 Nov 2026)

1. `RUN_PLAN_POLICY`: presets only on `research-public` and `mask-only`; free mode allowed for researchers on `on-premise`, and enabled on the lab instance from the start.
2. Presets are added only through reviewed pull requests for now; deployment-local presets are later work, out of scope.
3. Saved user plans: later, out of scope for the first release.
4. The first `stroke-lnm` presets are `full`, `mni-mask-analysis`, `ct-normalisation` and `lesion-qc-report`; `mni-mask-analysis` includes the report.
5. The report step must accept runs without tract disconnection, so that a QC-only report is possible: a report step change and a `stroke-lnm` minor version (card F1-26).
6. Reusing outputs of earlier runs as inputs: later, out of scope for the first free-mode release.
7. If an external tool's licence forbids publishing its image, deployments may build the wrapper locally without publishing it: a minimal exception to D7, documented in the design (section 4.3, *Local builds*).
8. `reference-tested` implementations may be enabled on the lab instance (`on-premise`), labelled experimental; never on public profiles.
9. Contract 1.2 is approved as described.
