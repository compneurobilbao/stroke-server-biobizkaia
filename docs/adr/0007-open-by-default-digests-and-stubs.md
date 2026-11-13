# ADR-0007: Open code, steps referenced by digest, stubs as fixtures

- Status: accepted
- Date: 2026-11-13
- Plan reference: decision D7 (decided 13 Nov 2026), cards F0-10, F0-24, F0-33

## Context

Some components could be kept private (for example a step whose source is not yet published), which would require private registries, private CI and credentials on every deployment. Parts of the pipeline do not exist yet (the functional-disconnection wrapper, segmentation, prediction, the SP2 simulator), but the platform, CI and UI must be built and tested end to end now.

## Decision

1. All platform code, steps and pipeline definitions are public.
2. Pipelines reference steps by fully qualified image name and digest; tags are not accepted in `pipeline.yaml`. A step name and version maps to exactly one digest. The registry design still allows a private registry with a read-only pull credential, but nothing uses it now.
3. *Stub* steps have the same manifest interface and output types as the step they stand in for, set `stub: true`, and produce deterministic synthetic outputs. They are used as CI fixtures and as placeholders. Pipelines may list them as allowed implementations; only CI or demo deployments may enable them; a slot whose only implementations are stubs has no default; any result produced by a stub is labelled synthetic and is never offered as a real analysis.
4. Steps whose real implementation does not exist yet stay unmounted in deployments (ADR-0015); nothing is built in-house where no lab code exists.

## Consequences

- No credentials are needed to pull images; on-premise installations can mirror images by digest.
- Digest pinning makes deployments reproducible and makes "which code produced this result" a lookup, at the cost of a pipeline version bump whenever an image is rebuilt.
- Every real step card also ships its stub (where a stub is useful), keeping CI independent of heavy tools and licensed data.

## Alternatives considered

- **Private steps in a private registry and private CI.** Rejected on 13 Nov 2026 (plan, F0-24 change): extra credentials and infrastructure, and a public CI that cannot run the whole pipeline.
- **Tags instead of digests.** Rejected: tags are mutable, so provenance would not identify the code that ran.
- **Mocking at the executor level instead of stub images.** Rejected: would bypass the contract, so CI would not exercise staging, mounts, provenance or output checks.
