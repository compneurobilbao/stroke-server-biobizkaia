# Configuration

Local configuration is defined by copying `.env.example` to `.env` and adjusting its values before starting the services.

```bash
cp .env.example .env
```

The `.env` file must not be committed. It may contain passwords, local paths or environment-specific commands.

## Main Variables

| Variable | Purpose | Default value |
| --- | --- | --- |
| `ENVIRONMENT` | Name of the runtime environment. | `development` |
| `POSTGRES_DB` | Database created by the PostgreSQL container. | `neuroimagen` |
| `POSTGRES_USER` | PostgreSQL user. | `neuroimagen` |
| `POSTGRES_PASSWORD` | PostgreSQL password. Must be changed outside development. | `neuroimagen` |
| `DATABASE_URL` | URL used by the API and the worker to connect to PostgreSQL. | `postgresql+psycopg://neuroimagen:neuroimagen@postgres:5432/neuroimagen` |
| `CELERY_BROKER_URL` | Redis broker used by Celery to enqueue tasks. | `redis://redis:6379/0` |
| `CELERY_RESULT_BACKEND` | Redis backend used by Celery for technical results. | `redis://redis:6379/1` |
| `STORAGE_ROOT` | Internal path where studies, results and logs are stored. | `/app/data/studies` |
| `ALLOWED_EXTENSIONS` | Extensions accepted when uploading studies. | `.nii,.nii.gz,.dcm,.zip,.tar,.tar.gz,.gz,.json,.txt` |
| `MAX_UPLOAD_SIZE_MB` | Maximum allowed file size, in MB. | `1024` |
| `PROCESSOR_NAME` | Technical name of the configured processor. | `dummy-development-processor` |
| `PROCESSOR_VERSION` | Technical version of the configured processor. | `0.1.0` |
| `PROCESSOR_BACKEND` | Active backend: `dummy` or `compneuro`. | `dummy` |
| `PROCESSOR_COMMAND` | Command template used by the `dummy` backend. | Development dummy |
| `WORKER_DOCKERFILE` | Dockerfile used to build the worker. | `backend/Dockerfile` |
| `WORKER_REPLICAS` | Replicas of the `worker` service used by `make up`/`make rebuild`. Can be set in `.env` or passed to the command. | `1` |
| `COMPNEURO_CONTAINER_IMAGE` | Base image documented for compneuro traceability. | `compneurobilbaolab/compneuro-anatproc:1.1` |
| `COMPNEURO_ANATPROC_REF` | Commit of the external repository used when building the compneuro worker. | `a2f3e7c9523ed521c3f85f7dffde5ee8fb400842` |
| `COMPNEURO_PROJECT_MOUNT` | Path that the external processor sees as the project. | `/project` |
| `COMPNEURO_COMMAND` | Command run by the compneuro adapter. | `bash /app/src/apreproc_launcher.sh` |
| `PROCESSING_TIMEOUT_SECONDS` | Processor timeout; `0` disables it. | `0` |
| `MAX_CONCURRENT_PROCESSING_JOBS` | Internal concurrency of each worker. For compneuro it must stay at `1`. | `1` |
| `GENERATE_OUTPUT_ZIP` | Generate a ZIP of the `Preproc` results. | `true` |
| `GENERATE_TECHNICAL_PDF` | Generate the technical processing PDF. | `true` |
| `GENERATE_RENDERED_PNG` | Render NIfTI results to PNG after `compneuro`. | `true` |
| `NIFTI_RENDERER` | CLI rendering tool. In compneuro it must be FSL `slicer`. | `slicer` |
| `NIFTI_RENDER_MAX_FILES` | Maximum number of NIfTI files to render per study. | `50` |
| `NIFTI_RENDER_TIMEOUT_SECONDS` | Timeout per rendered file. | `300` |
| `TECHNICAL_REPORT_FILENAME` | Name of the PDF inside `output/reports/`. | `technical_report.pdf` |
| `BIDS_VALIDATE` | Reserved for future formal BIDS validation. | `false` |
| `CORS_ORIGINS` | Origins allowed for browser calls. | `http://localhost,http://localhost:5173` |
| `AUTH_SECRET_KEY` | HMAC key used to sign JWT tokens. Must be changed outside development. | `change-me-in-production` |
| `AUTH_ACCESS_TOKEN_EXPIRE_MINUTES` | Lifetime of the local access token. | `480` |
| `SHARE_LINK_EXPIRE_HOURS` | Default expiry of temporary PDF download links. | `72` |
| `RETENTION_ENABLED` | Allows real deletion by retention. The dry run does not require it to be enabled. | `false` |
| `RETENTION_DAYS` | Minimum days of retention used by the retention CLI. | `90` |
| `APP_PUBLIC_BASE_URL` | Public URL included in notification emails. | a private address in the shipped example; use `http://<server-host>` |
| `NOTIFICATIONS_EMAIL_ENABLED` | Enables the SMTP delivery attempt in addition to internal notifications. | `true` in Docker Compose |
| `SMTP_HOST` | SMTP server used by the worker to send emails. | `mailpit` |
| `SMTP_PORT` | SMTP port. | `1025` |
| `SMTP_USERNAME` | SMTP user if the relay requires authentication. | empty |
| `SMTP_PASSWORD` | SMTP password or API key. Do not commit. | empty |
| `SMTP_FROM_EMAIL` | Sender authorised by the SMTP server. | `noreply@<sender-domain>` |
| `SMTP_USE_TLS` | Use STARTTLS before authenticating. | `false` with Mailpit |

## Pipeline runs

Only used when the Compose profile `pipelines` is enabled (see `docs/deployment.md`). Without the profile these variables have no effect.

| Variable | Purpose | Default value |
| --- | --- | --- |
| `COMPOSE_PROFILES` | Set to `pipelines` to start `pipeline-worker`, `beat` and `docker-proxy` with `make up`. | unset |
| `PIPELINES_DIR` | Directory scanned for `pipeline.yaml` inside the pipeline worker. | `/app/pipelines` |
| `STEP_MANIFESTS_DIR` | Directory scanned for `step.yaml`. | `/app/steps` |
| `BUNDLE_ROOT` | Reference bundle root. | `/app/data/bundles` |
| `ORCHESTRATOR_EXECUTOR` | Default step executor: `docker` or `subprocess`. | `docker` |
| `ORCHESTRATOR_STEP_EXECUTORS` | Per-step overrides, `step-name=executor,...`. | empty |
| `ORCHESTRATOR_POLL_SECONDS` | Poll interval of running attempts. | `5` |
| `ORCHESTRATOR_MAX_ATTEMPTS` | Attempts per slot per start or resume. | `2` |
| `ORCHESTRATOR_LEASE_SECONDS` | Heartbeat age after which another task may take a run over. | `600` |
| `ORCHESTRATOR_WORKER_CONCURRENCY` | Pipeline runs one `pipeline-worker` drives at once. | `2` |
| `PLATFORM_VERSION` | Platform version recorded on new runs. | `0.0.0-dev` |
| `DOCKER_PROXY_URL` | Base URL of the socket proxy. | `http://docker-proxy:2375` |
| `DOCKER_EXECUTOR_HOST_PATH_MAP` | `worker_path=host_path[,...]`; required, see "Host path mapping" in `docs/deployment.md`. | empty |
| `DOCKER_EXECUTOR_USER` | Numeric non-root user of step containers. | worker UID:GID |
| `DOCKER_EXECUTOR_PIDS_LIMIT` | Maximum processes per step container. | `512` |
| `DOCKER_EXECUTOR_PULL_MISSING` | Pull allow-listed images missing on the host. | `false` |
| `DOCKER_EXECUTOR_ALLOW_GPUS` | Grant GPUs to step containers. | `false` |

## Initial Admin User

There is no open public registration. After applying migrations, create or update the first admin with:

```bash
make create-admin EMAIL=admin@example.org
```

The command asks for the password on the console and runs `python -m app.cli.create_admin` inside the `api` container.

Local authentication uses a JWT signed with `AUTH_SECRET_KEY`. In development there is a default value, but in any shared environment you must set your own key and not commit it. The API refuses to start outside `development` if the key is still the default value or is too short.

Share links use opaque random tokens and store only their hash in the database. `SHARE_LINK_EXPIRE_HOURS` defines the default expiry when the user creates a link from the GUI or the API.

The retention policy is configured with `RETENTION_DAYS` and applied via CLI. Real execution is disabled by default with `RETENTION_ENABLED=false`; the command without `--execute` works as a dry run and lets you review candidates without deleting data:

```bash
make retention-dry-run DAYS=90
make retention-execute DAYS=90
```

`make retention-execute` only deletes if `RETENTION_ENABLED=true` is set in the environment of the `api` container.

Retention only affects terminal studies (`completed`, `failed`, `canceled`) and excludes `queued`, `processing`, already deleted studies and studies with `keep_forever`.

## SMTP Notifications

In development Docker Compose uses Mailpit by default. Mailpit captures emails at `http://localhost:8025` but does not deliver messages to the Internet. To send to Gmail or other real mailboxes you must use an authenticated SMTP relay or a domain with correct SPF/DKIM.

The `.env.example` and `.env.compneuro.example` files include the Brevo SMTP configuration prepared for this deployment, with `SMTP_PASSWORD` empty. The real SMTP key must be added only in the local `.env` and must never be committed.

Example configuration with Brevo:

```env
NOTIFICATIONS_EMAIL_ENABLED=true
SMTP_HOST=smtp-relay.brevo.com
SMTP_PORT=587
SMTP_USERNAME=brevo-smtp-user
SMTP_PASSWORD=smtp-key-generated-in-brevo
SMTP_FROM_EMAIL=sender-verified-in-brevo
SMTP_USE_TLS=true
```

`SMTP_FROM_EMAIL` must be a sender verified in Brevo or belong to a domain verified there. Do not use a sender address whose domain is not authorised with SPF/DKIM at the provider.

Sending is performed by the `worker` service when it closes a processing run. If the SMTP configuration changes, restart `api` and `worker`; restarting only the API does not update the process that sends the emails.

## `PROCESSOR_COMMAND`

`PROCESSOR_COMMAND` is the command template for the development processor (`PROCESSOR_BACKEND=dummy`). The API and the worker must not be coupled to the external script: the worker always invokes `processor_adapter`, and the adapter decides which command to run according to the active backend.

In `dummy` mode, the adapter runs `PROCESSOR_COMMAND`. In `compneuro` mode, the adapter does not use `PROCESSOR_COMMAND` as the main command but `COMPNEURO_COMMAND`.

Example:

```env
PROCESSOR_COMMAND=python /app/external_processor/dummy_processor.py --input {input_dir} --output {output_dir} --study-id {study_id}
```

Available placeholders:

- `{input_dir}`: directory with the uploaded file.
- `{output_dir}`: directory where the script must generate the PDF.
- `{study_id}`: UUID identifier of the study.
- `{logs_dir}`: directory reserved for technical logs.

In `dummy` mode, the configured script must generate at least one `.pdf` file inside `output_dir`. If it does not, the processing run is marked as failed.

## `PROCESSOR_BACKEND=compneuro`

The real integration runs `compneuro-anatproc` inside the worker, not through Docker-in-Docker. Minimum configuration:

```env
PROCESSOR_BACKEND=compneuro
PROCESSOR_NAME=compneuro-anatproc
PROCESSOR_VERSION=1.1
WORKER_DOCKERFILE=worker/Dockerfile.compneuro
ALLOWED_EXTENSIONS=.nii.gz
WORKER_REPLICAS=2
MAX_CONCURRENT_PROCESSING_JOBS=1
GENERATE_OUTPUT_ZIP=true
GENERATE_TECHNICAL_PDF=true
GENERATE_RENDERED_PNG=true
NIFTI_RENDERER=slicer
```

To run more than one `compneuro` processing at a time, scale replicas of the `worker` service instead of raising the container's internal concurrency. Example: `WORKER_REPLICAS=2` in `.env` or `make up WORKER_REPLICAS=2` allows two simultaneous processing runs if resources are sufficient. The effective capacity is shown in the admin dashboard.

In this mode the main command is `COMPNEURO_COMMAND`, by default `bash /app/src/apreproc_launcher.sh`. The worker is built on `compneurobilbaolab/compneuro-anatproc:1.1`, so it runs the launcher and FSL `slicer` inside the same worker container.

## `.env.compneuro.example` Template

`.env.compneuro.example` is the commented reference for preparing a real `.env` with `PROCESSOR_BACKEND=compneuro`. Each variable has an immediate comment with its purpose so that the configuration can be reviewed before starting services.

Main blocks:

- Environment and PostgreSQL: `ENVIRONMENT`, `POSTGRES_*`, `DATABASE_URL`.
- Celery/Redis queue: `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`.
- Storage and upload: `STORAGE_ROOT`, `ALLOWED_EXTENSIONS`, `MAX_UPLOAD_SIZE_MB`.
- Compneuro processor: `PROCESSOR_*`, `WORKER_DOCKERFILE`, `COMPNEURO_*`.
- Concurrency: `WORKER_REPLICAS` defines how many worker containers are started; `MAX_CONCURRENT_PROCESSING_JOBS` defines internal processes per container and must stay at `1` for compneuro.
- Technical artefacts: `GENERATE_*`, `NIFTI_*`, `TECHNICAL_REPORT_FILENAME`.
- API and security: `CORS_ORIGINS`, `AUTH_*`, `APP_PUBLIC_BASE_URL`.
- SMTP notifications: `NOTIFICATIONS_EMAIL_ENABLED`, `SMTP_*`.

Before copying it to `.env`, review in particular `POSTGRES_PASSWORD`, `AUTH_SECRET_KEY`, `APP_PUBLIC_BASE_URL`, `SMTP_USERNAME`, `SMTP_PASSWORD` and `SMTP_FROM_EMAIL`. The versioned examples must not contain real credentials.

`COMPNEURO_COMMAND` is the configurable point for changing the script while keeping the `compneuro` backend. If the new processor requires other dependencies, the change must come with another `WORKER_DOCKERFILE` or a different base image. The resulting container is still the `worker` service: it must have Celery, the platform's Python dependencies, access to `/app/data` and the configured command available inside the container.

To replace the processor more broadly, review:

- `PROCESSOR_BACKEND`: active backend that selects the adapter.
- `COMPNEURO_COMMAND` or the equivalent command of the new backend.
- `WORKER_DOCKERFILE`: Dockerfile that builds the worker with the necessary tools.
- `COMPNEURO_CONTAINER_IMAGE`/documented base image, if this backend is kept.
- the result structure expected by `worker/tasks.py` and `processor_adapter/output_packager.py`.

The API automatically prepares BIDS for a single T1w subject. If the subject is left empty, it generates a safe identifier. If an invalid value is provided, it responds `400`.

The local `compneuro-anatproc/` folder is not needed to run the integration. It may exist as a temporary reference ignored by Git, but the supported flow uses the Docker image and `worker/Dockerfile.compneuro`.

## How To Replace The Processor

To change the Docker image or use another script, keep the separation between platform and external processor:

1. Create or adapt a worker Dockerfile with Celery, the platform's Python dependencies and the new processor's tools.
2. Set `WORKER_DOCKERFILE` to build that worker.
3. Set `COMPNEURO_COMMAND` if it is enough to change the script inside the `compneuro` backend, or create a new backend in `processor_adapter` if the expected interface changes.
4. Make sure the new command reads the data prepared by the platform and writes results to the expected output, ideally `output/Preproc`.
5. Let the worker generate the platform's own artefacts: `output/reports/technical_report.pdf` and `output/outputs.zip`.
6. Rebuild and restart `worker`; restart `api` if shared configuration changes.

Neither the GUI nor the FastAPI endpoints need to be modified if the new processor respects the expected interface described in `docs/processing-pipeline.md`.

## Operational Security

- Do not commit `.env`.
- Do not put real secrets in `.env.example`.
- Change `POSTGRES_PASSWORD` outside local development.
- Do not use real identifying or sensitive data in tests or examples.
- Keep `ALLOWED_EXTENSIONS` limited to expected formats.
- Review `MAX_UPLOAD_SIZE_MB` according to disk capacity and operational policy.
- If the processor command or the worker Dockerfile changes, restart or rebuild the affected services.

## Relationship With Docker Compose

`docker-compose.yml` reads these variables and applies default values if they are not defined. In local development, creating `.env` from `.env.example` is usually enough; in real deployments, passwords, paths, CORS and the processor command must be reviewed before starting services.

`WORKER_REPLICAS` is not injected into the containers. `make up` and `make rebuild` read that key from `.env` if it exists, or from the command-line parameter if passed explicitly. Example: `make up WORKER_REPLICAS=2` runs `docker compose up -d --scale worker=2`.
