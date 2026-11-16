---
name: backend
description: Guide for backend work in this repository (FastAPI routers, SQLAlchemy 2 models, Alembic migrations, error codes, API tests). Use when changing backend/ or tests of the API.
---

# Backend

- FastAPI app in `backend/app/main.py`; routers by area in `backend/app/api/` (`auth`, `users`, `studies`, `shares`, `notifications`, `admin`, shared `helpers.py`); versioned case/run API in `backend/app/api/v1/` (card F0-30).
- SQLAlchemy 2 models in `backend/app/models/`, Pydantic schemas in `backend/app/schemas/`, services in `backend/app/services/`, settings in `backend/app/core/config.py`.
- Every schema change has an Alembic migration in `backend/alembic/versions/` with a working downgrade; CI runs upgrade/downgrade/upgrade on PostgreSQL. Migrations need Opus review; the data model design (ADR-0004, `docs/data-model.md`) needs owner approval to change.
- Errors: `api_error(ErrorCode.X)` from `backend/app/core/errors.py`; English `detail`, stable `code`; no stack traces to users.
- Keep endpoints small, validate input explicitly, enforce ownership, roles and quotas, audit relevant actions. Long work goes to the worker/orchestrator.
- Tests in `tests/` (`test_api.py` and friends); run `make lint` and `make test` in a POSIX environment; CI is the source of truth.
