# ADR-0015: Selectable implementations per pipeline slot

- Status: accepted
- Date: 2026-11-13
- Plan reference: decision D15 (decided 13 Nov 2026), cards F0-10, F0-30, F0-31, F0-33

## Context

For several pipeline positions there will be more than one usable tool: different segmenters (including external models), normalisation methods, disconnection methods, models of different versions, or a stub. Some positions have no implementation yet. Researchers want to choose, administrators need to control what a deployment offers, and every result must say which implementation produced it. Without a rule, each alternative would need its own pipeline definition.

## Decision

1. A pipeline is a chain of **slots** ([pipeline.md](../contracts/pipeline.md)). A slot is defined by its consumed and produced ports (name and artefact type), optional conditions, and whether it is optional.
2. A step declares `provides: <slot>` in its manifest. It can fill a slot when it matches the slot's ports under rules M1–M5 ([pipeline.md §4.1](../contracts/pipeline.md#41-matching-a-step-to-a-slot)); the validator and the orchestrator check this before anything runs.
3. `pipeline.yaml` lists per slot the *allowed* implementations, each pinned by image digest, with optional literal parameters, bindings to case metadata and bundle pins (so one image can appear as several configured variants), and an optional default.
4. A deployment *enables* a subset of the allowed implementations and may set its own default (configuration, card F0-33; admin endpoints, card F0-30). Stubs are enabled only in CI or demo deployments.
5. At run creation the user picks one enabled implementation per slot, or accepts the default (user choice, then deployment default, then pipeline default).
6. A slot with no enabled implementation is shown as unavailable. If it is optional, the run proceeds without it and dependent optional slots are skipped; if it is required, run creation is refused.
7. The run provenance records, for every slot, its status (selected, skipped, unavailable) with the reason, and for selected slots the implementation id, step name and version, image digest, stub flag, how it was chosen, and the bundles resolved.

## Consequences

- New tools, including wrappers around external models, are added by publishing a step and adding an allowed entry: a minor pipeline version, no code change.
- The case wizard shows one selector per slot with the deployment default preselected and marks unavailable slots (card F0-31).
- Results of different implementations are comparable because they are bound to the same artefact types; this places the burden of precise type semantics on the artefact type registry.
- Users choose implementations, not raw parameter values, in contract 1.0; alternative settings are offered as configured variants.

## Alternatives considered

- **One pipeline definition per combination of tools.** Rejected: combinatorial growth and duplicated wiring.
- **Free choice of any registered image at run time.** Rejected: unreviewed code would run on clinical-research data, and provenance would be harder to audit; the allowed list in a versioned pipeline is the review point.
- **Matching by artefact types only, without `provides`.** Rejected: two unrelated steps with the same types (for example two different maps on the working grid) could be swapped silently; `provides` states intent.
- **Plugin entry points inside the orchestrator.** Rejected: re-couples tools to the web repository (ADR-0001).
