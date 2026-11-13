# ADR-0016: Detailed data model design for cases, runs, step attempts and artefacts

- Status: proposed
- Date: 2026-11-13
- Plan reference: decision D4 (with D5, D8, D9, D10, D14, D15), card F0-11; implemented by F0-12, F0-15, F0-30

## Context

[ADR-0004](0004-data-model.md) fixes the entities of D4 and the constraints that the step contract 1.0 places on them, and leaves the entity-relationship design, the state machines and the permission matrix to card F0-11. Card F0-12 implements the design literally, so the design must say what each table holds, which transitions are allowed and who may trigger them, and how existing `Study` and `ProcessingJob` rows and endpoints survive. The full design is in [docs/data-model.md](../data-model.md); this record states the choices that matter for review.

## Decision

1. **Tables.** Phase 0 (migration 0011, F0-12): `cases`, `input_files`, `pipeline_runs`, `run_inputs`, `run_slots`, `run_slot_bundles`, `step_runs`, `artifacts`, `reference_bundles`, `reports`, `slot_implementation_settings`. Later cards create `case_grants` (F1-03), `model_versions` (F1-08), `review_records` (F1-04), `predictions` (F1-10), `observations` (E2-3), `sealed_reports` and `seal_openings` (E2-6) with the shapes fixed in the design document, so that Phase 0 tables do not change when they arrive.
2. **Runs, slots and attempts.** A `RunSlot` row records the resolution of each pipeline slot for a run (status, reason, implementation, image digest, stub flag, `chosen_by`, frozen effective parameters), mirroring `RunProvenance.slots[]`. A `StepRun` row is one execution attempt of a selected slot, mirroring `RunProvenance.steps[]`. Retry and resume create new attempts inside the same `PipelineRun`, which keeps its seed, choices and bundles.
3. **States.** Runs and step attempts use `queued`, `running`, `awaiting_review` (accepted by the constraints, reserved for F1-05), `succeeded`, `failed`, `canceled` (the contract's spelling). The step's exact `outcome` (`succeeded`, `rejected`, `failed`, `canceled`, `timed_out`, `contract_violation`) is a separate column constrained to agree with the status. `succeeded` is final; `failed` and `canceled` runs can be resumed, except after an input rejection. Every transition is a compare-and-set update. Cancel and resume are idempotent as specified in the design document (section 7.3). At most one run per case is active.
4. **Case metadata.** A case holds only a platform-generated `subject_label`, `sex` (`female`, `male` or absent), `age_band` (a 5-year band string such as `65-69`, validated against the code constant `AGE_BAND_WIDTH_YEARS = 5`; the server never receives an exact age) and the deployment profile. No names, dates, user-chosen file names or free-text identifiers are stored for new cases.
5. **Choices and accounting.** The user's request is kept as submitted (`requested_implementations`); the resolved choice is in `run_slots`; deployment enablement and defaults are rows in `slot_implementation_settings`, seeded from the registry configuration and changed through admin endpoints. Run accounting (`submitted_by`, `submit_as`, `billing_account`) and per-attempt usage are plain columns, so usage reports are SQL queries.
6. **Conventions.** UUID keys; `timestamptz` in new tables; enumerations as `varchar` with `CHECK` constraints instead of PostgreSQL `ENUM` types; format checks in PostgreSQL and in the API schemas; storage paths relative to the storage root; restrictive foreign keys except for the composition children of a run.
7. **Roles.** The permission matrix of the design document (section 9) covers `admin` and `researcher` now and the reserved `reviewer`, `coordinator` and `service` roles, which are accepted by a `CHECK` on `users.role` from migration 0011 and enforced from F1-03. Per-case access for non-owners comes from `case_grants`.
8. **Migration.** Three stages: 0011 creates the model and back-fills it from legacy rows with derived identifiers (`cases.id = pipeline_runs.id = studies.id`, `step_runs.id = processing_jobs.id`, other ids by UUIDv5), each legacy job becoming an attempt of a single `legacy` slot; between F0-15 and F0-30 the worker writes both models; 0012 reconciles, freezes the legacy tables and turns the legacy endpoints into deprecated aliases for one release; 0013 removes them and purges legacy identifying fields. 0011 and 0012 have tested downgrades; 0013 is irreversible and is rolled back by restoring the pre-migration backup.

## Consequences

- `RunProvenance` can be regenerated from the database with the field mapping in the design document (section 8), and the database can be checked against the file.
- The orchestrator (F0-15) owns all transitions after creation; the API only creates runs, requests cancellation and resumes. Duplicate messages and races resolve through the partial unique indexes and compare-and-set updates.
- F1-03, F1-05 and F1-08 need no change to Phase 0 tables: reserved states and roles are already accepted by the constraints.
- F0-12 needs a PostgreSQL service in CI for its migration test (open point in the design document).
- The worker carries dual-write code for one card interval, and the legacy aliases live for one release.

## Alternatives considered

- **One `StepRun` per slot, updated in place on retry.** Rejected: loses the history of attempts that the provenance records, and makes "who retried what" an audit-log question.
- **A new `PipelineRun` for every retry.** Rejected: the contract keeps the seed and the run identity on resume and retry, and the UI would show several runs for one analysis.
- **Slot resolution as a JSON column on `pipeline_runs`.** Rejected: "which runs used implementation X or bundle Y" must be an indexed query (recall of a faulty image or bundle).
- **PostgreSQL `ENUM` types.** Rejected: adding a value needs a non-transactional `ALTER TYPE` (already a pain in migration 0005), and the tests run on SQLite.
- **Big-bang migration that drops `studies` in F0-12.** Rejected: the worker and routes still use the legacy tables until F0-15 and F0-30, and the plan keeps legacy endpoints for one release.
- **Storing exact age or date of birth and deriving bands on demand.** Rejected: more identifying than needed; bands are all that steps and stratified calibration use.
