---
description: Run the checks that apply to the changed areas and report real results.
---

# Run quality checks

Run what applies to the changed areas (see `AGENTS.md`, "Validation before closing a task"):

- `make lint`, `make test` (backend, worker, adapter, orchestrator; needs a POSIX environment).
- `make test-steps` in its own virtualenv and `python contracts/validate.py` on changed manifests and examples (steps, SDK, contracts).
- From `frontend/`: `npm run typecheck`, `npm run lint`, `npm test`, `npm run build`.
- `make check-docs`, `make check-secrets`.

Report each command with its real result. Say which checks could not run here and why; CI on the PR is the source of truth.
