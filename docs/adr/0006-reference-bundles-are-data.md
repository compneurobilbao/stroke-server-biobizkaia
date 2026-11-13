# ADR-0006: Reference bundles and models are versioned data, not code

- Status: accepted
- Date: 2026-11-13
- Plan reference: decision D6 (with D10), cards F0-10, F0-18

## Context

Results depend as much on reference data as on code: normative connectomes, tract atlases, concept maps and their nulls, templates, model weights and calibration tables. These are large (up to tens of gigabytes), change on their own schedule, and some are under data use agreements that forbid redistribution. Today the `lnm` scripts read and even write inside a shared normative data tree, and nothing records which data a result came from.

## Decision

1. Reference data is packaged as immutable, versioned bundles described by `bundle.yaml` ([docs/contracts/bundles.md](../contracts/bundles.md)): id, semantic version, bundle type, licence (SPDX or `LicenseRef-…`, plus a `redistributable` flag), optional citation and metadata, a list of files with SHA-256 and size, and a content digest computed from that list.
2. Bundles never live in Git. They are registered and verified by the bundle registry (card F0-18) and mounted read-only at `/ref/<requirement name>/` in the step container.
3. Steps declare requirements by bundle type, optionally restricted to an id and a version range; pipelines or deployments may pin exact versions; the orchestrator refuses to run a step whose required bundle is missing or fails verification.
4. Models are bundles (D10): a model release is a bundle containing weights, feature specification, model card and calibration tables.
5. Every step records the bundles it read (id, version, content digest) in its provenance, and the run record lists the bundles resolved for each slot.
6. Small method-specific files that are part of an algorithm (for example the CT template and elastix parameter files shipped with CTLesion2MNI152) may stay inside the step image; they are covered by the image digest.

## Consequences

- A result can be traced to exact data versions; tampering or corruption is detected by checksums.
- Bundles that are not redistributable are excluded from release tarballs and on-premise installers and must be installed locally from the licensor.
- Bundle preparation (computing checksums, writing `bundle.yaml`) becomes a documented task for lab staff; the fixture generator (card F0-20) produces toy bundles for CI.
- Full re-hashing of large bundles is expensive; the registry verifies fully at registration and after copies, and cheaply (sizes, manifest hash) before each run.

## Alternatives considered

- **Data inside step images.** Rejected for large or independently versioned data: images of tens of gigabytes, a rebuild per data update, and no way to honour per-dataset licences.
- **Git LFS or DVC in the code repositories.** Rejected: mixes licensed data with public code, and Git hosting is the wrong place for data under a data use agreement.
- **Download on demand from public sources at run time.** Rejected: violates the no-network rule, breaks on-premise use and makes results depend on remote availability.
- **Unversioned shared directory (current practice).** Rejected: no provenance, no concurrency safety, no integrity check.
