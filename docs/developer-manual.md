# Developer Manual

## Local Environment

```bash
cp .env.example .env
make up
make test
```

The meaning of each `.env` variable is documented in `docs/configuration.md`.

## Backend

- FastAPI in `backend/app`.
- Models in `backend/app/models`.
- Pydantic schemas in `backend/app/schemas`.
- Alembic migrations in `backend/alembic`.
- The `GET /api/admin/dashboard` endpoint aggregates operational state for admins without touching the processing flow.

## Worker

- Celery in `worker/`.
- Do not add image analysis or medical interpretation to the worker; use `processor_adapter`.
- `worker/Dockerfile.compneuro` builds the real worker on top of the `compneuro-anatproc` image and adds the platform's dependencies.
- Technical post-processing runs in the same worker: it renders NIfTI with FSL `slicer`, generates the PDF and packages results.
- The recommended concurrency for `compneuro` is `1`, because the flow uses `/project` as a fixed path.

## Processor Adapter

- `processor_adapter.adapter.DummyProcessorAdapter`: keeps the development processor with a PDF.
- `processor_adapter.adapter.CompneuroAnatprocAdapter`: prepares `/project`, runs `src/apreproc_launcher.sh` and validates `Preproc/BET` and `Preproc/ProbTissue`.
- `processor_adapter.nifti_renderer`: finds `.nii`/`.nii.gz` in `Preproc` and runs `slicer input -a output.png`.
- `processor_adapter.technical_pdf_report`: generates the technical PDF with metadata, list of results and rendered PNGs.
- `processor_adapter.output_packager`: generates the results ZIP with relative paths.
- `processor_adapter.artifacts`: compatibility bridge for existing imports.

## BIDS

- `backend/app/services/bids.py` prepares BIDS for a single T1w per study.
- BIDS sessions are not used in this phase.
- `bids_subject_id` must have the format `sub-XXXX`; example: `sub-O01`.

## Frontend

- React/Vite in `frontend/`.
- Simple, clear UI (currently in Spanish) with no medical review flows that are not implemented.
- Install dependencies with `npm install` from `frontend/`; `package-lock.json` pins the resolved versions for reproducibility.
- Validate frontend changes with `npm run lint`; the configuration lives in `frontend/eslint.config.js` and uses the ESLint 9 flat config format.
- If deployed with Docker/Nginx and the frontend changes, rebuild the container with `make frontend-rebuild`.

## Conventions

- Conventional Commits.
- Update tests on functional changes.
- Update docs if architecture, API, deployment or processing flow change.
- Document frontend tooling changes when they affect installation, lint or component structure.

## Basic Multi-User Support

The platform includes local login with JWT, `admin`/`researcher` roles, a per-study owner and a history filtered by permissions.

Main points:

- `backend/app/models/user.py`: `User` model, roles and the `system` user.
- `backend/app/services/auth.py`: PBKDF2 hashing, JWT, current-user and admin dependencies.
- `backend/app/cli/create_admin.py`: creation or update of the initial admin.
- `backend/app/cli/retention.py`: dry run and explicit execution of study retention.
- `backend/alembic/versions/0004_multiuser_auth.py`: `users` table, `owner_user_id` and `actor_user_id`.
- `frontend/src/main.jsx`: login, session, "my studies" view and basic admin user management.

Create the first admin:

```bash
make create-admin EMAIL=admin@example.org
```

Basic job management includes truncated logs, cancellation of queued or running jobs, retry of failed ones and safe deletion. The admin dashboard already shows the global state of the queue, jobs, storage, services, users and studies by status. Local backup and restore, secure sharing of PDF reports, notifications, per-user quotas, technical review of results and initial retention via CLI are already available. The next recommended phase is multiple upload and batches.

Minimum expected permissions:

- `admin`: can see all studies, manage users and access the administrative view.
- `researcher`: can upload, list, view and download only their own studies.
- Unauthenticated user: cannot access sensitive endpoints.

Outside the current implementation:

- Google/OIDC.
- ORCID.
- Complete `viewer` role.
- ZIP sharing through links.
- Advanced institutional email.
- Automatic retention.
- Multiple upload.
- 2FA.
- Formal clinical review with reviewer, date, comments and validated external criteria.

## How To Make Changes

Use this table before modifying the system. The idea is to change the minimum that is correct and keep code, tests and docs aligned.

| Change | Review | Validate |
| --- | --- | --- |
| API endpoint, permissions or JSON response | `backend/app/api/routes.py`, `backend/app/schemas/`, `docs/api.md`, tests in `tests/test_api.py` | `make test`, `make lint` |
| Persistent model | `backend/app/models/`, Alembic migration, `docs/architecture.md`, tests | `make test`, `make lint` |
| Worker or processing states | `worker/tasks.py`, `processor_adapter/`, `docs/processing-pipeline.md`, `docs/operations-manual.md` | `make test`, `make lint` |
| External processor or worker Dockerfile | `processor_adapter/`, `worker/Dockerfile.compneuro`, `docs/configuration.md`, `docs/deployment.md` | `make test`, `make lint`, manual test if using the real `compneuro` |
| Frontend | `frontend/src/main.jsx`, `frontend/src/styles.css`, `frontend/README.md`, user manual if the flow changes | `npm run lint` from `frontend/` |
| Configuration or `.env` variables | `.env.example`, `docker-compose.yml`, `backend/app/core/config.py`, `docs/configuration.md` | `make check-secrets`, `make check-docs` |
| Documentation only | affected README/doc and cross-links | `make check-docs` |

Do not read or paste `.env` in conversations or commits. Do not use real identifying data in fixtures, examples or screenshots.
