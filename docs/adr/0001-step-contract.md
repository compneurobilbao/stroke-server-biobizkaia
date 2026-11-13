# ADR-0001: Every processing tool is a step behind one contract

- Status: accepted
- Date: 2026-11-13
- Plan reference: decision D1, card F0-10

## Context

The current platform runs one processor per worker build: the worker image derives from the processor image and `worker/tasks.py` branches on the backend name (plan finding A1). `CompneuroAnatprocAdapter` serialises jobs with a global file lock and a repointed `/project` symlink (A2), and provenance is three strings set from environment variables (A4). The stroke pipeline needs a chain of tools with incompatible toolchains (FSL and elastix; MRtrix3 and ANTs; PyTorch; Python 3.12 scientific stacks), run on the web server under Docker and on the HPC under Apptainer, with a provenance record that names every image digest, parameter and reference dataset. Tools come from several repositories and, later, from SP2.

## Decision

1. A step is a container image plus a manifest `step.yaml`, specified in [docs/contracts/step-contract.md](../contracts/step-contract.md) (contract 1.0) with JSON Schemas in `contracts/schemas/v1/`.
2. The platform invokes every step as `/usr/local/bin/run --in /in --out /out --params /in/params.json`. A step reads `/in` and `/ref` (read-only) and writes only `/out` and `/tmp`; it has no network, runs as an arbitrary non-root user and behaves identically under Docker and Apptainer.
3. The manifest declares identity and version, the slot it provides (ADR-0015), typed input and output ports (artefact types from a shared registry), a JSON Schema for parameters with defaults, reference bundle requirements, resources including GPU, declared error codes, licences, and a reference test with expected outputs and tolerances.
4. Exit codes: 0 success; 10 input rejected or QC failure, with a user-facing `/out/error.json`; anything else is an internal failure.
5. Every step writes `/out/provenance.json` (inputs and outputs with SHA-256, effective parameters, bundles, software versions, timings). The platform wraps it in a run-level record that adds what the container cannot know: image digest, executor, granted resources, slot choice and accounting ([provenance.md](../contracts/provenance.md)).
6. The contract is versioned (`contract_version`, currently `1.0`); compatibility rules are in [versioning.md](../contracts/versioning.md). `contracts/validate.py` is the reference validator.

## Consequences

- Any tool, in any language, becomes usable by writing a thin wrapper and a manifest; the web repository no longer changes when a tool is added. Steps can be run by lab scripts on the HPC without the platform.
- The worker image becomes a slim orchestrator image (card F0-15); the global lock and symlink handling disappear for new steps.
- Each existing tool needs a wrapper (cards F0-22…F0-27), an image that tolerates a read-only root and an arbitrary UID, and a reference test. CTLesion2MNI152's unchecked `subprocess.call` and the `lnm` scripts' writes into the reference tree must be fixed in their wrappers.
- Provenance becomes complete and checkable by hash, at the cost of the platform verifying inputs, outputs and bundle digests after each step.
- Contract changes are now costly by design: they need a new ADR and a version bump (plan section 7).

## Alternatives considered

- **Per-tool adapter classes in the web repository** (the current pattern). Rejected: couples web releases to tool releases, cannot host incompatible toolchains in one worker image, and each adapter reinvents I/O and error handling.
- **BIDS Apps CLI as-is** (`<bids_dir> <output_dir> participant`). Rejected: no manifest, no typed outputs, no parameter schema, no provenance, and inputs addressed by subject rather than by role. The chosen CLI is deliberately close to it so that BIDS App authors find it familiar.
- **Boutiques descriptors.** A mature JSON descriptor for command-line tools with typed inputs. Not chosen: it describes arbitrary command lines rather than one fixed invocation, has no notion of slots, reference bundles or provenance records, and would still need everything in this contract on top. Steps can still be described with Boutiques for other uses.
- **CWL CommandLineTool.** Expressive and standard, but brings a workflow-engine data model and runner semantics that duplicate ADR-0002, and does not cover provenance content, slots or bundles. Rejected for the same scope reason.
- **Steps receiving BIDS paths and subject IDs.** Rejected: puts identifiers into containers and makes every step parse BIDS; the case store names files instead.
