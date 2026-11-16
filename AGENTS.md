# AGENTS.md

Main instructions for AI agents working in this repository (OpenCode, Claude Code and any other agent). Read this file before touching code or documentation. `CLAUDE.md`, `.claude/`, `.opencode/` and `docs/ai-development-rules.md` only point back here; if anything disagrees, this file and the plan win, and verifiable code wins over prose.

## What this project is

`stroke-server`: a research platform that runs stroke neuroimaging pipelines (lesion QC, normalisation to MNI152, functional and structural disconnection, cognitive profiles, reports) on a web server and on an HPC cluster, with full provenance. It is research software: every output is "research use only, not for clinical decisions".

## Sources of truth (read before working)

| What | Where |
|---|---|
| Implementation plan: architecture decisions D1-D15, phases, **task cards**, test data policy (section 6), review and deployment protocol (section 7) | [`docs/plan/IMPLEMENTATION_PLAN.md`](docs/plan/IMPLEMENTATION_PLAN.md) |
| Step contract 1.x (normative): `step.yaml`, `pipeline.yaml`, `provenance.json`, `bundle.yaml`, `error.json`, artefact types, versioning | [`docs/contracts/`](docs/contracts/README.md), start with [`docs/contracts/step-contract.md`](docs/contracts/step-contract.md) |
| Contract JSON Schemas, examples and validator | [`contracts/`](contracts/) (`schemas/v1/`, `examples/`, `artefact-types.yaml`, `validate.py`) |
| Architecture decision records | [`docs/adr/`](docs/adr/README.md) |
| Tests: what exists, how to run them, CI jobs, `make e2e` | [`docs/testing.md`](docs/testing.md) |
| How to build a step (SDK and template) | [`steps/README.md`](steps/README.md) |
| Current architecture and processing flow | [`docs/architecture.md`](docs/architecture.md), [`docs/processing-pipeline.md`](docs/processing-pipeline.md) |
| Index of the other docs | [`docs/README.md`](docs/README.md) |

You normally work on one card of the plan. Read the card (search the plan for its ID, e.g. `F0-13`), the decisions and cards it depends on, section 6 and section 7.

## Repository layout

- `backend/`: FastAPI app (`app/main.py`), SQLAlchemy 2 models (`app/models/`), Pydantic schemas (`app/schemas/`), services (`app/services/`), CLIs (`app/cli/`), Alembic migrations (`alembic/versions/`). HTTP routers are split by area in `backend/app/api/`: `auth.py`, `users.py`, `studies.py`, `shares.py`, `notifications.py`, `admin.py`, with shared code in `helpers.py`; the versioned case and run API goes into `backend/app/api/v1/` (card F0-30).
- `worker/`: Celery app and tasks; long work never runs inside an HTTP request.
- `orchestrator/executors/`: the executor interface (`base.py`: `Executor` with `submit`, `poll`, `cancel`, `collect`, `cleanup`) and the `subprocess` backend. The `docker` and `slurm` backends and the pipeline orchestrator are added by their cards.
- `processor_adapter/`: legacy boundary to the dummy processor and `compneuro-anatproc`, run through the executor interface by `executor_bridge.py`. Kept during the migration; do not add new adapter classes here (new tools are steps).
- `external_processor/`: dummy development processor, no scientific or clinical validity.
- `steps/`: step SDK (`steps/sdk/`, package `stroke_step_sdk`), the template step (`steps/template/`) and the steps of this repository (e.g. `steps/lesion-qc/`).
- `contracts/`: machine-readable side of the step contract (see above).
- `fixtures/`: deterministic synthetic test data generator (`python -m fixtures.generate`) and its committed output in `fixtures/data/`; licences in `fixtures/LICENCES.md`.
- `frontend/`: React + TypeScript + Vite UI; routed feature modules in `src/features/`, API client in `src/api/`, i18n in `src/i18n/` (English source strings, Spanish catalogue).
- `infra/reverse-proxy/`: Nginx entry point. `docker-compose.yml`: the stack. `worker/Dockerfile.compneuro`: optional worker image for the legacy anatproc path.
- `scripts/`: smoke, seed, backup and restore, docs and secrets checks, `e2e_openneuro_t1w.py`.
- `tests/`: backend, worker, adapter, executor, migration, contract and fixture tests.
- `data/studies/`: local case storage, ignored by Git.
- `.github/workflows/`: `ci.yml` (backend lint and pytest, Alembic on PostgreSQL, frontend typecheck/lint/test/build, docs and secrets checks, gitleaks, Docker builds) and `steps.yml` (SDK and step tests, step images under Docker and Apptainer).

## Commands

```bash
cp .env.example .env     # local setup; never read, print or commit .env
make up / make down      # start / stop the Docker Compose stack (make logs follows the logs)
make rebuild             # rebuild and recreate all services
make migrate             # alembic upgrade head inside the api container
make test                # pytest tests (PYTHONPATH=.:backend)
make test-steps          # step SDK and step tests; separate virtualenv, see the Makefile comment and steps/README.md
make lint / make format  # ruff check / ruff format on backend worker processor_adapter tests
make check-docs          # required docs present
make check-secrets       # basic secret scan (gitleaks also runs in CI and in pre-commit)
make smoke               # healthcheck through the proxy on a running stack
make e2e                 # one case end to end on a running instance with a public CC0 OpenNeuro T1w;
                         # needs E2E_EMAIL and E2E_PASSWORD of a dedicated test account (environment only)
python contracts/validate.py FILE...   # validate step, pipeline, bundle, provenance and error documents
```

Frontend, from `frontend/`: `npm ci`, `npm run dev`, `npm run typecheck`, `npm run lint`, `npm test`, `npm run build`.

The Python test suite needs a POSIX system (Linux, macOS, WSL or a container); it does not import on native Windows. CI runs Python 3.12.

## Rules

### Scope

- **Follow the plan's cards.** Implement what your card asks, inside its "Touches" list and nothing else; parallel cards never share a "Touches" area. Features, refactors or files outside the card are proposed as follow-up card stubs in the PR, not implemented. Work that matches no card needs a card (a plan change) approved by the project owner first.
- The plan's model assignment (section 4) and the "Opus review" markers on cards apply as written.
- Prefer small, reviewable changes. Do not revert changes made by other people or other agents.

### Processing steps

- A processing tool enters the platform **only as a step that follows the step contract** (`docs/contracts/`, ADR-0001): a container image plus `step.yaml`, the fixed command `run --in /in --out /out --params /in/params.json`, typed inputs and outputs from `contracts/artefact-types.yaml`, `provenance.json`, exit codes 0 / 10 / other. Build steps with the SDK and the template in `steps/`; validate manifests with `contracts/validate.py`.
- Pipelines are declarative `pipeline.yaml` files with slots (D2, D15), run by our orchestrator through executors (D3). Do not wire a tool into the API or the worker directly, and do not add per-tool adapter classes.
- Reference data and model weights are versioned bundles (`bundle.yaml`, ADR-0006) mounted read-only; they never go into Git.
- When wrapping an existing scientific tool, do not change its algorithm unless the card says so. The legacy `compneuro-anatproc` integration treats that repository as a versioned black box and only runs `src/apreproc_launcher.sh`.
- Changing the contract after F0-10 needs a new ADR, a version bump (`docs/contracts/versioning.md`) and owner approval.

### Data

- **No patient data, ever.** Do not use, request, download or copy real clinical images, masks or records, nor data from any lesion database. Use the synthetic fixtures (`fixtures/`) or public de-identified data whose licence allows redistribution, and record the licence (`fixtures/LICENCES.md`, plan section 6).
- Failures that only show on real data are reported by lab staff with identifiers stripped; reproduce them on synthetic data before fixing.
- On a server, never read or copy anything in the case store other than the synthetic case you created.

### Language and public-repository hygiene

- English everywhere: code, identifiers, comments, docstrings, log and error messages, docs, commit messages and PR text. The UI is English by default; Spanish (and later other languages) comes from the i18n catalogues in `frontend/src/i18n/`, and report templates may have Spanish versions. Do not add Spanish text anywhere else.
- This is a public repository: technical content only. No people's names, no server, host or institution details, no grant or funding references. Refer to the modelling partner only as "SP2".
- No secrets: do not read `.env` or credential files, do not commit secrets, keep `.env` ignored and `.env.example` free of real values.

### Safety

- Do not run destructive commands (`make clean`, `docker compose down -v`, `rm -rf`, destructive migrations, history rewrites, force pushes) without explicit owner permission.
- Add or update tests with every functional change; update the docs affected by the change in the same PR.

## Git, branches and review (plan section 7)

- `develop` is the integration branch. One card = one branch from `develop` (`feat/<card-id>-<slug>` unless your brief names another) = one PR against `develop`. Conventional Commits.
- `main` receives releases only: it is a fast-forward of tagged `develop` commits. Agents never push to `main`.
- Agent instruction files (`AGENTS.md`, `CLAUDE.md`, `.claude/`, `.opencode/`, `opencode.json`, `docs/ai-development-rules.md`, `docs/agentic-workflow-audit.md`) live on both branches like any other file.
- **Agents never merge** pull requests, their own or anyone else's. **CI is the source of truth**: a PR is not done until every check is green; if you cannot run something locally, say so and rely on CI.
- Definition of done: acceptance criteria met and shown in the PR (commands and output); CI green; docs updated; no new Spanish text; no secrets; "Touches" respected; changelog entry (when `CHANGELOG.md` is within the card's scope).
- PR description sections: What was done / What was not done / How it was verified / Follow-ups proposed (as card stubs); add "Decisions taken beyond the plan" when there are any.
- Opus review is mandatory where the card says so and for any PR that changes `contracts/`, `orchestrator/`, auth, migrations, de-identification, sealing, statistical code or `AGENTS.md`. The reviewer gets the diff and the card, not the implementer's summary; findings are fixed in the same PR.
- **Owner approval is required** for ADRs, design changes (data model, architecture decisions), contract changes, plan changes and every deployment.

## Deployment

- Only with explicit approval from the project owner for that specific tag; never on an agent's own initiative.
- Only from a tagged commit on `develop` (later `main`). Sequence: check that no real jobs are running → `make backup` → pull images by digest → migrate → smoke test → synthetic end-to-end case → record versions in `docs/operations-manual.md`. Rollback: previous tag, plus restore of the backup if a migration ran.
- Server access uses a dedicated deploy account with minimum rights.

## Validation before closing a task

- Backend, worker, adapter, orchestrator: `make lint` and `make test`.
- Steps, SDK, contracts: `make test-steps` (own virtualenv) and `python contracts/validate.py` on every changed manifest or example.
- Frontend: `npm run typecheck`, `npm run lint`, `npm test`, `npm run build`.
- Docs or agent files: `make check-docs`; check links and coherence with this file and the plan.
- Security or configuration: `make check-secrets`.
- Review `git diff` before committing; report which checks ran, their real result, and which could not run.

## Agent tooling

- `CLAUDE.md` and `.claude/` (commands and skills) serve Claude Code; `opencode.json` and `.opencode/` (agents, commands, skills, secret-protection plugin) serve OpenCode. Both only refine this file.
- Record changes to agent tooling, permissions or these rules in `docs/agentic-workflow-audit.md`.
