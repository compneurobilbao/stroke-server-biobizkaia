---
name: repo-orientation
description: Find your way in this repository: sources of truth, layout, commands and sensitive areas.
compatibility: opencode
---

## When to use

Load this skill when you start a task in this repository or need to rebuild context.

## Flow

1. Read `AGENTS.md`.
2. Read your card in `docs/plan/IMPLEMENTATION_PLAN.md` (search for its ID), plus sections 2 (decisions), 6 (test data) and 7 (review protocol).
3. For processing work, read `docs/contracts/step-contract.md`, `steps/README.md` and the relevant ADRs in `docs/adr/`.
4. For the current system, read `docs/architecture.md`, `docs/processing-pipeline.md` and `docs/testing.md`.
5. Confirm commands in `Makefile`, `frontend/package.json`, `.github/workflows/` and `docker-compose.yml`.
6. Place the change in the right folder (`backend/app/api/<area>.py`, `backend/app/models/`, `worker/`, `orchestrator/executors/`, `steps/<step>/`, `contracts/`, `fixtures/`, `frontend/src/features/`, `infra/`, `docs/`), and only inside the card's "Touches" list.
7. Ignore for normal context: `.git/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `node_modules/`, `dist/`, `data/studies/`, `.e2e-cache/` and `.opencode/node_modules/`.

## Reminder

Processing tools enter only as steps under the step contract. `processor_adapter/` is the legacy boundary; do not add adapters there.
