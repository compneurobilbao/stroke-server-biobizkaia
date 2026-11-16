---
description: Add or change a FastAPI endpoint with schemas, tests and API docs.
---

# Implement a backend endpoint

Endpoint or card: $ARGUMENTS

- Put the route in the router of its area in `backend/app/api/` (`auth.py`, `users.py`, `studies.py`, `shares.py`, `notifications.py`, `admin.py`; shared helpers in `helpers.py`). New case and run endpoints go under `backend/app/api/v1/` (card F0-30). Do not change paths or signatures of existing endpoints unless the card says so.
- Request and response models in `backend/app/schemas/`; database access through the models in `backend/app/models/`; schema changes need an Alembic migration with a tested downgrade.
- Error responses: English `detail` plus a stable machine-readable `code`, built with `api_error(ErrorCode.X)` from `backend/app/core/errors.py` (add new codes to its catalogue); never return stack traces.
- Enforce ownership, roles and quotas as the existing endpoints do; audit relevant actions.
- Long work never runs inside the request: enqueue it for the worker or orchestrator.
- Add API tests in `tests/` (success, validation errors, each permission boundary) and update `docs/api.md`.
- Check: `make lint`, `make test` (POSIX environment; CI is the source of truth).
