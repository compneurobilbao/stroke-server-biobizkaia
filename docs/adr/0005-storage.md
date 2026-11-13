# ADR-0005: Filesystem case store with BIDS raw data and per-step derivatives

- Status: accepted
- Date: 2026-11-13
- Plan reference: decision D5, cards F0-10, F0-17

## Context

Steps exchange NIfTI, JSON, TSV, PDF and PNG files; the HPC executor copies inputs out and outputs back; the viewer reads artefacts directly; the platform runs on a single host per deployment. Researchers expect BIDS layouts, and the provenance of derivatives must be traceable to the step version that produced them. The current storage class (`LocalStudyStorage`) already isolates paths.

## Decision

1. Storage stays on the filesystem behind the existing storage class.
2. Per case: a BIDS raw tree (`sub-<id>/ses-acute/<datatype>/…`) holding the input files, and `derivatives/<step name>-<step version>/…` per step run, each with a `dataset_description.json` whose `GeneratedBy` names the step and image digest.
3. Stored file names are built by the case store from the case's entities and the artefact type's BIDS fields ([artefact-types.md §4](../contracts/artefact-types.md#4-naming-in-the-case-store)); steps only ever see `/in/<port><ext>` and `/out/<port><ext>`.
4. Each step's original `provenance.json` is stored unchanged in its derivatives directory, and the run-level `RunProvenance` file is written next to the run's derivatives and updated after each step ([provenance.md §5](../contracts/provenance.md#5-run-level-file-in-the-case-store)).
5. Checksums recorded in provenance are verified when staging inputs and when collecting outputs.

## Consequences

- Derivatives are browsable with standard BIDS tools; the BIDS validator runs in CI (card F0-17).
- Staging to the HPC is a plain directory copy of `/in`; the slurm executor needs no knowledge of BIDS.
- Retention and deletion operate on directories per case and per run.
- Scaling beyond one host requires a new storage backend; the storage class keeps that door open.

## Alternatives considered

- **Object storage (MinIO/S3).** Rejected for now: no need on a single host, extra service to operate in hospitals, and every step would need staging to and from a local disk anyway.
- **Database large objects.** Rejected: poor fit for multi-gigabyte images and for tools that read files.
- **Flat per-run directories without BIDS naming.** Simpler, but loses interoperability with BIDS tooling and makes exported derivatives hard to reuse; naming is cheap because it is done in one place.
