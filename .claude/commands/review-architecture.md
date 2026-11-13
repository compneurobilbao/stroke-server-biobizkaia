---
description: Review a change or area against the plan's architecture decisions and the step contract.
---

# Review architecture

Scope: $ARGUMENTS

Check, against `docs/plan/IMPLEMENTATION_PLAN.md` (section 2), `docs/contracts/` and `docs/adr/`:

- Separation of API, worker/orchestrator, executors, steps, frontend and infrastructure.
- Processing tools only as contract steps; no tool wired into the API or worker; no new adapters in `processor_adapter/`.
- Provenance and traceability: inputs hashed, image digests, bundles and parameters recorded.
- Reference data and models as bundles, not code.
- Basic security (auth, ownership, path handling, secrets) and the public-repository rules.
- Scope: the change stays inside its card's "Touches" list.
- Coherence between docs and code.

Return prioritised findings with file references. Do not edit files.
