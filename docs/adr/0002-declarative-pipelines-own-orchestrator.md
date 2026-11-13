# ADR-0002: Pipelines are small declarative DAGs run by our own orchestrator on Celery

- Status: accepted
- Date: 2026-11-13
- Plan reference: decision D2, cards F0-10, F0-15

## Context

Each clinical analysis is a short chain of 6–10 mostly linear steps per case (plan section 2.3), with a few conditional branches (skip normalisation when an MNI mask is supplied) and optional parts (functional disconnection is unavailable until its step exists). The platform already runs Celery with Redis and keeps state in PostgreSQL. Runs need per-step status for the UI, resume from the last successful step, cancel and retry per step, and a provenance record. The platform is intended to be documented as software of unknown provenance (SOUP) items in a quality file later, so every added dependency has a documentation cost.

## Decision

1. A pipeline is declared in `pipeline.yaml` ([docs/contracts/pipeline.md](../contracts/pipeline.md)): run inputs and accepted input sets, slots with consumed and produced artefact types, wiring of outputs to inputs, conditions on run-input presence, optional slots, and the allowed implementations per slot (ADR-0015).
2. Our own orchestrator (card F0-15), running as Celery tasks, loads and validates the file, resolves the run plan once at run creation, persists `PipelineRun` and `StepRun` rows, runs steps through executors (ADR-0003) in dependency order, and supports resume, cancel and per-step retry.
3. Conditions are evaluated only at run creation, so the set of steps a run will attempt is known before it starts. A static availability check rejects pipelines whose wiring cannot be satisfied for some input set.
4. Any step failure stops the run in contract 1.0; the user retries the failed step.

## Consequences

- One scheduler and one state store (Celery, PostgreSQL); orchestration state is in our own tables, visible to the API and auditable.
- We own the code for ordering, resume, cancellation and retries. It is small for linear DAGs but must be tested carefully (card F0-15 lists the required tests).
- Data-parallel cohort processing does not belong in the platform: lab scripts on the HPC call the same step containers directly.
- New pipeline features (conditions on intermediate results, per-slot failure tolerance, fan-out) are contract changes and need an ADR.

## Alternatives considered

- **Nextflow.** Excellent for data-parallel HPC workflows and supports Docker and Apptainer, but brings a second scheduler, its own state and work directories, a JVM runtime and a large SOUP item. Its strengths (channels, fan-out over thousands of samples) are not needed for one case at a time.
- **Snakemake.** File-based rules map well to neuroimaging, but its execution model is a batch run over a target set, not a long-lived service with per-step status, cancellation and resume driven by a web API; embedding it would mean wrapping its state in ours.
- **Airflow, Prefect, Dagster.** General-purpose orchestrators with their own databases, schedulers and UIs; heavy to deploy in hospitals (on-premise profile) and duplicative of Celery/PostgreSQL.
- **CWL with a runner (cwltool, Toil).** Standard and portable, but adds a runner and a data model beside ours and does not express slots with selectable implementations.
- **Hard-coded sequences in Python.** Simplest at first, but every pipeline change becomes a code release, and the UI and validator cannot inspect the graph. Rejected.

Revisit if pipelines become wide or data-parallel inside the platform.
