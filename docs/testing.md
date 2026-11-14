# Testing

Current tests:

- API healthcheck.
- study creation through upload.
- extension validation.
- adapter with the dummy processor.
- detection of the generated PDF.
- external script failure.
- output without PDF.
- validation and generation of the BIDS subject.
- BIDS preparation for a single T1w.
- compneuro adapter in simulated mode.
- search for `.nii` and `.nii.gz` in `Preproc`.
- generation of FSL `slicer` commands without running real FSL.
- generation of the technical PDF with dummy PNGs and NIfTI names.
- download of the technical PDF from the API.
- behaviour without NIfTI and with a rendering failure.
- generation of the technical PDF and ZIP.
- correct and incorrect login.
- endpoint protection for unauthenticated users.
- `admin`/`researcher` permissions.
- history filtered by owner.
- download allowed for the owner and blocked for non-owners.
- audit of login and upload with the acting user.
- study detail with jobs.
- truncated logs.
- cancellation of queued jobs.
- cancellation of running jobs with Celery task termination.
- cancellation is detected only through `cancel_requested_at`, never through the text of `error_message`.
- retry of failed jobs.
- soft delete and physical deletion of the study folder.
- deletion blocked for studies being processed.
- admin dashboard with operational summary.
- admin dashboard blocked for `researcher`.
- shared PDF links with creation, revocation, expiry and auditing.
- per-user storage quotas.
- notification preferences, listing and marking of one's own notifications.
- technical review `technical_only`/`reviewed`/`validated` according to permissions.
- worker behaviour when a running processor is cancelled.
- SMTP with STARTTLS before authentication.

Run:

```bash
make test
```

Python lint:

```bash
make lint
```

Frontend validation:

```bash
cd frontend
npm install
npm run lint
```

The frontend lint uses `frontend/eslint.config.js`, compatible with ESLint 9. `package-lock.json` must be kept up to date when frontend dependencies change.

The build is not run automatically by project decision. If the frontend bundle needs to be checked manually, use `npm run build` from `frontend/`; if Docker/Nginx is used, rebuild with `make frontend-rebuild`.

## Slow Manual Test

The real run of `compneuro-anatproc` with `sub-O01_T1w.nii.gz` is not part of `make test`, because it can take many minutes and requires the heavy image. It must be tested manually with:

```env
PROCESSOR_BACKEND=compneuro
WORKER_DOCKERFILE=worker/Dockerfile.compneuro
ALLOWED_EXTENSIONS=.nii.gz
WORKER_REPLICAS=2
MAX_CONCURRENT_PROCESSING_JOBS=1
```

Local reference file: `compneuro-anatproc/data/sub-O01/anat/sub-O01_T1w.nii.gz`.

If that local folder does not exist, use any anonymised T1w `.nii.gz` available. `compneuro-anatproc/` is not a permanent dependency of the project; the real integration uses the configured Docker image.

## Manual Test Performed

In a local test with the rebuilt compneuro worker, an example T1w `.nii.gz` was processed. The study ended in `completed` state and produced `output/Preproc`, 18 PNGs in `output/rendered_png/`, `output/reports/technical_report.pdf` and `output/outputs.zip`, with no rendering warnings recorded.

The multi-user tests do not require the real `compneuro` flow; they use a temporary SQLite database, the dummy processor and simulated Celery tasks.

## Continuous integration

GitHub Actions (`.github/workflows/ci.yml`) runs on every pull request and on pushes to `develop` and `main`. The jobs are:

- `backend`: `make lint` (ruff) and `make test` (pytest). The suite uses SQLite and in-memory Celery, so no Postgres or Redis service is needed (the PostgreSQL runs are the `tests-postgres` job).
- `migrations-postgres`: starts a `postgres:16-alpine` service container (same major version as `docker-compose.yml`), runs `alembic upgrade head`, `alembic downgrade base` and `alembic upgrade head` again from `backend/`, publishes the output in the job summary, then runs `pytest tests -m postgres`. Tests that need PostgreSQL carry the `postgres` marker and read the connection URL from `TEST_POSTGRES_URL`; without that variable they are skipped, so `make test` stays SQLite-only. New migration tests go in `tests/test_migrations_postgres.py` (or other tests with the marker) and run in this job. To run locally, start any PostgreSQL 16 instance and `export TEST_POSTGRES_URL=postgresql+psycopg://user:pass@localhost:5432/db`; the tests reset the `public` schema, so use a scratch database.
- `tests-postgres`: runs `tests/test_api_v1.py`, `tests/test_api_v1_hardening.py`, `tests/test_orchestrator.py`, `tests/test_orchestrator_legacy.py`, `tests/test_data_model.py`, `tests/test_bundles.py`, `tests/test_seed_implementations.py` and `tests/test_postgres_concurrency.py` against a `postgres:16-alpine` service with `TEST_DB=postgres`. The same files run on SQLite in `make test` (fast path); only the database behind them changes. See "Running the suites on PostgreSQL" below.
- `bids-validator`: `tests/test_bids.py` writes a synthetic case tree (raw inputs from `fixtures/`, two step derivatives, run provenance) to `BIDS_TREE_EXPORT`; the job then runs the pinned BIDS validator (JSR `@bids/validator`, version in the job's `BIDS_VALIDATOR_VERSION`, run with a pinned Deno) on the raw dataset and on every derivative dataset. The standard has no CT datatype and no `statmap`, `qc` or `report` suffixes, so the case store lists such files in each dataset's `.bidsignore` (card F0-17, `backend/app/services/bids.py`); everything else is validated. Locally: `BIDS_TREE_EXPORT=/tmp/bids PYTHONPATH=.:backend pytest tests/test_bids.py -k test_export_tree_for_bids_validator`, then `deno run -A jsr:@bids/validator@<version> <dataset>`.
- `frontend`: `npm ci`, `npm run lint` and `npm run build` in `frontend/`.
- `frontend-e2e` ("Frontend Playwright (mocked API)"): the Playwright `mocked` project of `frontend/e2e/mocked/` against the built frontend, with every `/api` request answered in the browser (seconds, no backend). See "Frontend end-to-end tests" below.
- `repo-checks`: `make check-docs` and `make check-secrets`.
- `gitleaks`: scan of the full git history with a pinned gitleaks release binary (checksum verified) and `.gitleaks.toml`.
- `docker-build`: builds the backend and frontend images without pushing. The compneuro worker image is not built in CI because it needs a large external base image.

The step SDK and template tests (`steps/`) are not part of `make test`: they use their own pinned dependencies (`steps/sdk/requirements-image.txt`, e.g. a different numpy pin than `backend/requirements.txt`) and need the SDK installed. Run them with `make test-steps` in a separate virtualenv; CI runs them in `.github/workflows/steps.yml`. Synthetic fixtures are already checked by `tests/test_fixtures.py`, which `make test` collects.

All of these can be reproduced locally with the make targets above. Third-party actions are pinned to exact versions.

## Running the suites on PostgreSQL

SQLite does not enforce foreign keys or `SELECT ... FOR UPDATE`, so a suite that passes there can still hide an insert-order bug or a missing lock. Tests that need a database build it through `tests/pgdb.py` (`new_engine(tmp_path, name)`), which has two modes:

- default: a SQLite file under `tmp_path`, built with `Base.metadata.create_all` (fast, no services);
- `TEST_DB=postgres` with `TEST_POSTGRES_URL` pointing at a server where the user may create databases: the first call of the pytest process creates a template database and runs `alembic upgrade head` on it, so the schema is the production one (constraints, partial unique indexes, foreign keys); every test then gets its own database cloned with `CREATE DATABASE ... TEMPLATE`, and `tests/conftest.py` drops it after the test and the template at the end of the session. Commits made by the code under test therefore never leak between tests, and several sessions can work on the same database.

Locally (any PostgreSQL 16, for example `docker run --rm -e POSTGRES_PASSWORD=p -p 5432:5432 postgres:16-alpine`):

```bash
export TEST_DB=postgres TEST_POSTGRES_URL=postgresql+psycopg://postgres:p@localhost:5432/postgres
PYTHONPATH=.:backend pytest tests/test_api_v1.py tests/test_api_v1_hardening.py tests/test_orchestrator.py   tests/test_orchestrator_legacy.py tests/test_data_model.py tests/test_bundles.py   tests/test_seed_implementations.py tests/test_postgres_concurrency.py
```

New tests that open their own engine should use `pgdb.new_engine` rather than `create_engine("sqlite:///...")` so they can run in both modes; add their file to the `tests-postgres` job when the behaviour depends on constraints or locks. Do not rely in a test on SQLite leniency (random foreign key values, ids of rows that do not exist).

`tests/test_postgres_concurrency.py` (skipped unless `TEST_DB=postgres`) runs real concurrent requests against the v1 locks (`lock_case`, `lock_user`): a gate pauses the first request right after it took its row lock, the second request is started and must block on it (checked through `pg_stat_activity`), then both outcomes are asserted. Scenarios: upload vs run creation (both orders), delete vs run creation (both orders), two uploads of one owner against the quota (gated and ungated), the lock itself with `lock_timeout`, and an audit-row check of every v1 write (each `audit_events` row references rows that exist, which PostgreSQL enforces and SQLite does not).

## Frontend end-to-end tests (Playwright)

`frontend/e2e/` holds the browser tests of the case wizard and the run page (cards F0-31 and F0-31a); `@playwright/test` is pinned in `frontend/package.json` and the lockfile. `frontend/playwright.config.ts` defines two projects:

- `mocked` (`e2e/mocked/`): the API is mocked in the browser from `docs/api/openapi-v1.json`; the job `frontend-e2e` of `ci.yml` runs it on every pull request. It stays a separate, fast job because it needs no images and no stack, and it checks things the real stack cannot produce on demand (failed, canceled and retried runs, slot states, request bodies). Locally: `cd frontend && npm ci && npx playwright install chromium && npm run build && npx playwright test --project=mocked`.
- `real-stack` (`e2e/real/`, only when `E2E_REAL_STACK=1`): the same wizard against the real Docker Compose stack, nothing mocked. The workflow `.github/workflows/e2e-real-stack.yml` (job `e2e-real-stack`; runs on pull requests and pushes that touch the frontend, backend, workers, orchestrator, pipelines, steps, contracts, fixtures, infra, Compose files or `scripts/e2e/`) builds the steps as `e2e-stroke-lnm.yml` does (this repository's steps and the external ones at the commits pinned in `pipelines/stroke-lnm/registry.yaml`, pushed to a throwaway registry for digests), mounts the materialised pipeline, the step manifests read from the images and the toy bundles through `./data/e2e`, starts `docker compose --profile pipelines up` (postgres, redis, api, legacy worker, frontend, reverse proxy, pipeline-worker, beat and the socket proxy, with the docker executor and `DOCKER_EXECUTOR_HOST_PATH_MAP`), registers the bundles, runs `seed_implementations --profile ci`, creates a throwaway admin and researcher with credentials generated in the job (masked, never committed), and runs Playwright through the reverse proxy, logging in through the form. The helper steps are in `scripts/e2e/real_stack.sh` (test only; it overwrites `.env`).

What the real-stack specs assert:

- MNI-mask mode, default implementations: the slot selector shows the `ci` defaults (functional disconnection is the stub), the run reaches `succeeded`, normalisation is skipped, every other slot succeeded, the artefact list holds the tract table, the synthetic functional-disconnection map and the report, the PDF report downloads and starts with `%PDF-`, and a step log is readable.
- MNI-mask mode with `report` set to `research-report-es`: only that choice is sent, the run records it as chosen by the user, and it succeeds.
- CT mode: the normalisation slot is unavailable (the CT step is not merged, so no profile mounts it), the wizard explains it and Start is disabled. No completed run is expected; when the CT step is merged and mounted this spec changes into a second finished run.

On failure the job uploads the Playwright report and traces (they contain what the browser typed; the credentials exist only on that runner) and the Compose logs. A run of the real steps takes a few minutes on top of the image builds, which dominate the job time; the Docker layers are not cached between runs.

## End-to-end test with a public T1w image

`make e2e` (or `python3 scripts/e2e_openneuro_t1w.py`) runs one real case through a running instance: it downloads `sub-01_T1w.nii.gz` from OpenNeuro dataset ds000001 (licence CC0, SHA-256 pinned in the script, cached in `.e2e-cache/`), uploads it through the API, waits for the job, checks that the PDF report is a valid PDF and deletes the test study (`--keep` keeps it). It needs a dedicated test account whose credentials are passed as `E2E_EMAIL` and `E2E_PASSWORD` environment variables (never as arguments); `E2E_BASE_URL` defaults to `http://localhost`. Exit code 0 means passed. It is not part of CI because it needs a running instance with the real processor.

