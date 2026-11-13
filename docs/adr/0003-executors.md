# ADR-0003: One executor interface with subprocess, docker and slurm backends

- Status: accepted
- Date: 2026-11-13
- Plan reference: decision D3, cards F0-10, F0-13, F0-14, F0-16

## Context

Light steps (intake, report) can run on the web server; heavy steps (tractography, GPU segmentation, large normative cohorts) need the institutional HPC, which runs a batch scheduler and, as far as is known, Apptainer rather than Docker. Hospitals running the on-premise profile have Docker only and no outbound connections. During migration the dummy processor and the existing anatproc integration must keep working. Facts about the HPC (scheduler, reachability, shared filesystem, accounts) are not yet verified.

## Decision

1. Executors implement one interface (`submit`, `poll`, `cancel`, `collect`, `cleanup`; card F0-13) and receive an executor request that carries the step image, staged inputs, bundle mounts, granted resources and an `accounting` record (`submitted_by`, `submit_as`, `billing_account`; [provenance.md](../contracts/provenance.md#4-accounting)).
2. Three backends:
   - `subprocess`: a command inside the worker image (dummy and legacy processors only);
   - `docker`: sibling containers started through a socket proxy restricted to create/start/wait/logs/remove on allow-listed images;
   - `slurm`: stages `/in` over SSH/rsync, submits `sbatch` wrapping `apptainer exec` (rather than `apptainer run`, so that the image's runscript is ignored as the step contract requires), polls `sacct` from a Celery beat task, pulls `/out` back and cleans the staging directory. The HPC never calls the web server.
3. The executor for each step is deployment configuration, not part of the step manifest. All backends apply the runtime rules of [step-contract.md §2 and §6](../contracts/step-contract.md#6-container-constraints-docker-and-apptainer): read-only image, arbitrary UID, explicit bind mounts, clean environment, no network, `SIGTERM` then `SIGKILL` after 30 seconds.
4. Each execution is recorded in the run provenance `execution` block: executor, runtime, image digest, SIF hash under Apptainer, external job ID, timestamps, exit code, outcome, granted resources, and usage (core-hours, GPU-hours).
5. Only the `service-account` submission policy is implemented in Phase 0; `per-user` and `chargeback` are declared in the interface.

## Consequences

- The same step image runs on a laptop, the lab server and the HPC; answers about the HPC change configuration, not architecture.
- The docker backend needs a socket proxy and an allow-list; it must never expose the full Docker API to the worker.
- The slurm backend owns staging, idempotent re-polling after restarts and cleanup; it is the most complex component and is tested against a shim in CI.
- Image conversion to SIF is an operational step (card F0-42); provenance records both the OCI digest and the SIF hash.

## Alternatives considered

- **Celery workers on HPC nodes.** Rejected: long-lived daemons on a batch cluster, outbound Redis access from the HPC, and jobs outside the scheduler's accounting.
- **Kubernetes (with Jobs, or Argo).** Rejected: no cluster is available, and it is too heavy for hospital installations.
- **HPC-side pull agent (cron fetching staged jobs).** Kept as the fallback if the HPC cannot be reached from the web server (plan risk table); it would be a fourth backend behind the same interface and needs its own ADR.
- **Docker-in-Docker for the worker.** Rejected: privileged containers and no benefit over a restricted socket proxy.
- **Executor chosen in the step manifest.** Rejected: where a step runs depends on the deployment (on-premise has no HPC), not on the step.
