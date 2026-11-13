# ADR-0004: Case-centred data model with pipeline runs, step runs and typed artefacts

- Status: accepted
- Date: 2026-11-13
- Plan reference: decision D4, cards F0-10, F0-11, F0-12

## Context

Today a `Study` is one uploaded file with one `ProcessingJob`, one PDF and one ZIP (plan finding A3), and provenance is three strings (A4). The stroke pipeline needs cases with several inputs (CT plus mask, or an MNI mask alone), multi-step runs, typed artefacts with checksums, reference bundle and model versions, and later reviews, sealed reports, observations and predictions. Existing rows and endpoints must survive the transition.

## Decision

1. New entities, at the level of the plan: `Case`, `InputFile`, `PipelineRun`, `StepRun`, `Artifact`, `ReferenceBundle`, `ModelVersion`, `Report`, `ReviewRecord`; later `SealedReport`, `Observation`, `Prediction`. Existing `Study` and `ProcessingJob` rows are migrated to `Case` and `PipelineRun` with a single legacy step; old endpoints stay as thin aliases for one release.
2. The contract fixes the following constraints that the detailed design (card F0-11) MUST respect:
   - `InputFile.role` values are the pipeline input names (`ct`, `lesion_native`, `lesion_mni`, and `mri` later); a run's set of roles selects one `input_set` ([pipeline.md §2](../contracts/pipeline.md#2-input-sets)).
   - Case metadata used by parameter bindings is stored so that it can be rendered as `case.sex` (`female`, `male`, or absent) and `case.age_band` (`"<lo>-<hi>"` in whole years, or absent).
   - `Artifact` rows carry the artefact type name from the registry, the producing `StepRun` and port, SHA-256 and size.
   - `StepRun` stores the fields of the provenance `execution` block (executor, runtime, image digest, external job ID, attempt, timestamps, exit code, outcome, granted resources, usage) and the slot and implementation chosen.
   - `PipelineRun` stores the pipeline id, version and file hash, the input set, the run seed and the accounting record.
3. The full entity-relationship design, state machines and permission matrix are decided in a separate ADR by card F0-11.

## Consequences

- The run provenance file (ADR-0005) can be regenerated from the database, and the database can be checked against the file.
- The migration must back-fill `Case`/`PipelineRun` from existing rows and keep legacy endpoints green (card F0-12).
- Identifiers in the database are pseudonymous; containers never see them (step contract §2.2).

## Alternatives considered

- **Extend `Study` with JSON columns for steps and artefacts.** Rejected: no referential integrity, no querying by artefact type or bundle version, and the state machines of runs and steps would live in untyped blobs.
- **Store provenance only as files.** Rejected: the UI, quotas, audit and retention need queryable state; files remain the portable record.
- **Generic workflow-engine schema (e.g. adopting a third-party engine's tables).** Rejected together with external orchestrators in ADR-0002.
