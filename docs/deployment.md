# Deployment

The initial deployment is designed for a single server with Docker Compose.

## Local Installation

1. Clone or open the repository.
2. Create the local configuration with `cp .env.example .env`.
3. Review passwords, paths and processor according to `docs/configuration.md`.
4. Start the services with `make up`.
5. Create the first admin with `make create-admin EMAIL=admin@example.org`.
6. Open the GUI at `http://localhost` or Swagger at `http://localhost/api/docs`.

Do not use real identifying or sensitive data in this initial version.

```mermaid
flowchart TB
  Internet --> Nginx[Nginx :80]
  Nginx --> Frontend[frontend]
  Nginx --> API[api :8000]
  API --> Postgres[(postgres)]
  API --> Redis[(redis)]
  Worker[worker] --> Redis
  Worker --> Postgres
  API --> Data[(./data)]
  Worker --> Data
```

## Compneuro Worker

To run `compneuro-anatproc`, the worker is built with `worker/Dockerfile.compneuro`, derived from `compneurobilbaolab/compneuro-anatproc:1.1`. That container holds Celery, the platform code and the neuroimaging tools. Docker-in-Docker is not used.

No separate `compneuro-anatproc` container is started. The `worker` service is the container that inherits that base image and runs `src/apreproc_launcher.sh` directly.

If another script or a different image is used in the future, the pattern stays the same: the `worker` service must contain Celery, the platform code, the processor's dependencies and access to the `./data:/app/data` volume. The new command must be configured through environment variables and respect the expected result structure documented in `docs/processing-pipeline.md`.

Minimum variables:

```env
PROCESSOR_BACKEND=compneuro
PROCESSOR_NAME=compneuro-anatproc
PROCESSOR_VERSION=1.1
WORKER_DOCKERFILE=worker/Dockerfile.compneuro
ALLOWED_EXTENSIONS=.nii.gz
WORKER_REPLICAS=2
MAX_CONCURRENT_PROCESSING_JOBS=1
```

`MAX_CONCURRENT_PROCESSING_JOBS` must stay at `1` for `compneuro`, because each worker container manages an internal `/project` path. To increase parallelism, scale worker containers with `WORKER_REPLICAS=2` in `.env`, `make up WORKER_REPLICAS=2` or `make rebuild WORKER_REPLICAS=2`. The expected simultaneous capacity is the number of active replicas.

The `.env.compneuro.example` template is commented variable by variable. Use it as a deployment checklist: copy it to `.env`, review secrets, confirm compneuro paths/commands and then start with `make up`.

`api` and `worker` must share `./data:/app/data`, because the API prepares BIDS and the worker writes `output/Preproc`, logs, the technical PDF and the ZIP.

## Docker step executor and socket proxy

The `docker` executor (`orchestrator/executors/docker.py`, ADR-0003) runs each step attempt as a sibling container. The worker never mounts the Docker socket and never runs with Docker privileges: it reaches the engine only through the `docker-proxy` service (`tecnativa/docker-socket-proxy`, pinned by digest), which is in the `docker-executor` Compose profile and attached to an internal network:

```bash
docker compose --profile docker-executor up -d docker-proxy
```

```mermaid
flowchart LR
  Worker[worker / orchestrator] -- "HTTP :2375, internal network" --> Proxy[docker-proxy]
  Proxy -- "/var/run/docker.sock" --> Engine[Docker engine on the host]
  Engine --> Step["step container (sibling)"]
  Data[("data directory on the host")] -. bind mounts .-> Step
```

Status: the executor is wired into the `pipeline-worker` service of the `pipelines` profile (see "Pipeline runs" below). The proxy alone is still available with `--profile docker-executor`.

### What the proxy restricts, and what it does not

The proxy enables only the API sections the executor needs: `CONTAINERS`, `IMAGES`, `POST` and `DELETE`. Exec, volumes, networks, build, commit, swarm, secrets, system and events answer `403`. The proxy filters by path and method, not by request body, so it cannot stop a caller from asking for a privileged container. That guarantee comes from the executor, which builds the container specification itself from a fixed template:

- the image must be on the allow-list given by the caller (the pipeline registry) and is checked before Docker is contacted; the local image must carry the pinned `repository@sha256:<digest>` and is started by its content ID;
- fixed contract command `/usr/local/bin/run --in /in --out /out --params /in/params.json` (the image's `ENTRYPOINT` and `CMD` are replaced);
- read-only root file system; `/in` and `/ref/<bundle>` read-only; `/out` and `/tmp` writable per-attempt directories; no other mounts;
- non-root numeric user (the worker's own UID by default), `--cap-drop ALL`, `no-new-privileges`, `--network none`, PID limit, CPU and memory limits with swap disabled, an init process so that `SIGTERM` reaches the step;
- cancel and timeout send `SIGTERM`, and the next `poll` after 30 seconds sends `SIGKILL`;
- the final state is saved under `<work_dir>/.executor/` and logs are copied to `<work_dir>/logs/step.log` (first and last 5 MiB if longer than 10 MiB) before the container is removed, so `poll`, `collect` and `cleanup` work from a persisted handle after a worker restart.

The worker is therefore trusted code. Anyone who can run arbitrary code in the worker, or reach port 2375 on the internal network, can start containers on the host. Do not publish the proxy port, and do not attach other services to `executor-net`.

### Configuration

`DockerExecutor.from_env(allowed_images)` reads:

| Variable | Default | Meaning |
|---|---|---|
| `DOCKER_PROXY_URL` | `http://docker-proxy:2375` | Base URL of the socket proxy |
| `DOCKER_EXECUTOR_HOST_PATH_MAP` | empty | `worker_path=host_path[,...]`: translates the worker's view of the data directory to the host's, because bind-mount sources are resolved by the engine on the host. Example: `/app/data=/srv/stroke/data` |
| `DOCKER_EXECUTOR_USER` | worker's UID:GID (`10001:10001` if the worker runs as root) | Numeric non-root user of the step container |
| `DOCKER_EXECUTOR_PIDS_LIMIT` | `512` | Maximum processes per step container |
| `DOCKER_EXECUTOR_PULL_MISSING` | `false` | Pull an allow-listed image that is absent on the host (needs registry access from the host); by default images must be pre-pulled |
| `DOCKER_EXECUTOR_ALLOW_GPUS` | `false` | Grant GPUs through device requests (not exercised in CI) |

The per-attempt directory (`work_dir`) must be on a path that exists on the Docker host (after mapping), and the container user must be able to traverse its parents.

### Tests

`tests/test_docker_executor.py` covers the executor against a fake engine API. `tests/test_docker_executor_integration.py` runs the template step image end to end through a real proxy; the `docker-executor` CI job builds the image, pushes it to a throw-away local registry to obtain a digest, starts the proxy and runs it. To run it locally, start the proxy with the settings of `docker-compose.yml` on a published loopback port, push `steps/template/Dockerfile`'s image to a registry, and set `STROKE_TEST_DOCKER_PROXY_URL` and `STROKE_TEST_STEP_IMAGE`.

## Pipeline runs (opt-in `pipelines` profile)

Pipeline runs are executed by the orchestrator (`run_pipeline`, `recover_pipeline_runs`). They are deployed behind the Compose profile `pipelines`. Without it, `docker compose config`, `make up` and every running service are exactly the legacy stack: `api`, `worker` (default queue, `process_study`), `postgres`, `redis`, `mailpit`, `frontend`, `reverse-proxy`.

Enable it by either of:

```bash
docker compose --profile pipelines up -d --build
# or, once, in .env (then `make up` includes the profile):
COMPOSE_PROFILES=pipelines
```

The profile adds three services:

| Service | Image | Role |
|---|---|---|
| `pipeline-worker` | `worker/Dockerfile` (slim, no processor inside) | Celery worker consuming **only** the `pipelines` queue. Attached to the default network (PostgreSQL, Redis) and to `executor-net` (to reach `docker-proxy`). Concurrency is `ORCHESTRATOR_WORKER_CONCURRENCY` (default `2`, one run per slot). |
| `beat` | `worker/Dockerfile` | Celery beat; every 5 minutes it enqueues `recover_pipeline_runs`. **Run exactly one instance**; do not scale it. |
| `docker-proxy` | pinned socket proxy | See above. Also part of the `docker-executor` profile. |

Queue separation: `worker.celery_app` routes `run_pipeline` and `recover_pipeline_runs` to the queue `pipelines`; every other task, including `process_study`, stays on the default queue. The legacy `worker` is started without `-Q` and keeps consuming only the default queue, so long pipeline runs cannot block legacy uploads. Messages for `pipelines` enqueued while no `pipeline-worker` runs wait in Redis until one starts.

```mermaid
flowchart LR
  API[api] -- "process_study (default queue)" --> Worker[worker, legacy]
  API -- "run_pipeline (pipelines queue)" --> PW[pipeline-worker]
  Beat[beat] -- "recover_pipeline_runs" --> PW
  PW -- "executor-net" --> Proxy[docker-proxy]
```

Mounts of `pipeline-worker`: `./data:/app/data` (cases, work directories and reference bundles under `BUNDLE_ROOT`, default `/app/data/bundles`), `./pipelines:/app/pipelines:ro` and `./steps:/app/steps:ro` (registries read from `PIPELINES_DIR` and `STEP_MANIFESTS_DIR`).

### Host path mapping (required)

Step containers are created by the Docker engine on the host, which resolves bind-mount sources on the host file system, not inside the worker. Set `DOCKER_EXECUTOR_HOST_PATH_MAP` to the absolute host path of `./data`:

```env
DOCKER_EXECUTOR_HOST_PATH_MAP=/app/data=/srv/stroke/data
```

If it is empty or wrong, step containers start with missing or empty inputs. The path must exist on the host, and the step user must be able to traverse its parents. Reference bundles live under the same data directory, so one mapping covers both.

### Security note

`docker-proxy` mounts the host Docker socket (the `:ro` flag on the socket file does not limit the API). It exposes only container and image calls, but it cannot inspect request bodies. Whoever controls `pipeline-worker` (arbitrary code in it, a compromised dependency, or access to port 2375 on `executor-net`) can create containers on the host, which is equivalent to elevated access to the host. Therefore:

- keep the profile off on instances that do not run pipelines;
- never publish the proxy port and never attach another service to `executor-net`;
- pipelines and step manifests are mounted read-only, and the executor only starts images pinned by digest in the loaded pipelines (see the restrictions above);
- restrict who can edit `./pipelines`, `./steps` and the worker image.

### Variables

All variables are listed in `.env.example` and described in `docs/configuration.md` (section "Pipeline runs"). The orchestrator ones (`PIPELINES_DIR`, `STEP_MANIFESTS_DIR`, `BUNDLE_ROOT`, `ORCHESTRATOR_*`) are defined in `docs/processing-pipeline.md`.

### Enabling pipelines on a host (API side)

The `api` service reads pipelines and step manifests through its own variables, so that it behaves exactly as before while they are unset: `PIPELINES_DIR` and `STEP_MANIFESTS_DIR` of the `api` default to `/app/pipelines-disabled` and `/app/steps-disabled` (directories that do not exist), and `GET /api/v1/pipelines` answers an empty list. `./pipelines` and `./steps` are always mounted read-only into `api` (inert unless pointed at), `./data` (and so `BUNDLE_ROOT`, default `/app/data/bundles`) is the mount it already had, and `DEPLOYMENT_PROFILE` (default `research-public`) is passed through.

Steps, in order:

1. **Profile and API variables.** In `.env`: `COMPOSE_PROFILES=pipelines`, `API_PIPELINES_DIR=/app/pipelines`, `API_STEP_MANIFESTS_DIR=/app/steps`. Then `make up` (or `make rebuild`) and `make migrate`.
2. **Host path map.** Set `DOCKER_EXECUTOR_HOST_PATH_MAP` as described above.
3. **Pipelines and manifests.** `./pipelines/<id>/` must hold `pipeline.yaml` and `registry.yaml` with the **published image digests** (`python pipelines/stroke-lnm/registry.py materialize --digests FILE --out DIR`, then use `DIR` as the mounted `pipelines` tree), and `./steps` (or another directory mounted over `/app/steps`) must hold the `step.yaml` read from each image (`/opt/step/step.yaml`). The repository tree has placeholder digests and only the manifests of the steps built here, so a step whose manifest is missing is listed with `available: false`.
4. **Reference bundles.** Register each bundle the enabled steps need into the database store, from inside `api` (the bundle directory must be under `./data`, so the container sees it):

   ```bash
   docker compose exec api python -m orchestrator.bundles --store db --root /app/data/bundles register /app/data/incoming/<bundle-dir>
   docker compose exec api python -m orchestrator.bundles --store db --root /app/data/bundles list
   ```

5. **Seed the implementation settings** (once; safe to repeat):

   ```bash
   make seed-implementations PROFILE=deployment ARGS=--dry-run   # preview
   make seed-implementations PROFILE=deployment
   ```

   This runs `python -m app.cli.seed_implementations --profile deployment` in `api`. It reads `<PIPELINES_DIR>/<id>/registry.yaml` and writes one `slot_implementation_settings` row per allowed implementation, enabled as the profile says, and one `preset_settings` row per preset file in `<id>/presets/` (enabled and default as `profiles.<profile>.presets` says). Stubs and planned entries are never enabled under `deployment` (the command refuses a registry that does so). Existing rows are not touched, so changes an admin made with `PUT /api/v1/admin/implementations/...` survive a re-run; `--reset` overwrites every row with the profile. The command logs each row (`insert`, `update`, `unchanged`, `warning`) and warns about enabled implementations whose image digest is still a placeholder. The command also refuses a profile that enables an implementation below the deployment's minimum validation level (`implementation_not_validated`; the minimum comes from `DEPLOYMENT_PROFILE` and `MIN_IMPLEMENTATION_VALIDATION`, and is `unvalidated` for the `ci` profile). No step is `validated` yet (see "Validation levels" below), so on `research-public` and `mask-only` (minimum `validated`) this refuses the whole `deployment` profile; `ARGS=--skip-below-minimum` seeds every implementation below the minimum disabled, with a warning, and the presets whose required slots are then empty disabled too, which leaves a public deployment with nothing to offer until validation records exist. `--profile ci` (stubs) is refused on those two profiles unless `ARGS=--allow-ci-profile` is given. Rows already enabled in the database below the minimum are reported as `WARNING` lines and left alone (plans and runs ignore them); `--reset` disables them.
6. **Use `/api/v1`.** `GET /api/v1/pipelines` and `.../versions/<v>/slots` show what is enabled; admins refine it through `/api/v1/admin/implementations`; researchers create cases, upload inputs and start runs (`docs/api.md`). Restart `api` after changing the mounted pipelines or manifests: they are loaded once per process.

#### Enabling free mode on an on-premise (lab) instance

Free mode (a run with outputs chosen by the user instead of a reviewed preset) is refused everywhere by default. A lab instance enables it in this order; do not set `RUN_PLAN_POLICY` first.

1. **Set the profile first.** In `.env`: `DEPLOYMENT_PROFILE=on-premise`, then `make up` (or `make rebuild`). The profile is read when the API starts. It changes three things:
   * new v1 cases are stamped with the profile `on-premise` (cases already created keep theirs);
   * the default policy becomes `free_for_researchers` (admins and researchers may use free mode, nobody else), instead of `presets_only`;
   * the minimum validation level for external tools is the lab level (`reference-tested`, shown as experimental), instead of `validated` as on the public profiles (design: `docs/design/run-plans.md` 4.4). It is enforced since card F1-23, at three points: `PUT /api/v1/admin/implementations/...` and `seed_implementations` refuse to enable an implementation below the minimum with `implementation_not_validated`, and plan previews, run creation and the slot listings treat an implementation below the minimum as not enabled whatever the settings table says (a user choice of one is refused with `implementation_not_validated`). `MIN_IMPLEMENTATION_VALIDATION` can raise the lab minimum to `validated`; it cannot lower it on the public profiles (the API refuses to start). Every real step is recorded in `registry.yaml` as `reference-tested`, shown as experimental in the slot listing, the admin list, the run page and the wizard, so **the lab instance must run with `DEPLOYMENT_PROFILE=on-premise`** (`research-public` and `mask-only` enable nothing until validation records exist).
2. **Optionally narrow or fix the policy.** Leave `RUN_PLAN_POLICY` empty to take the profile default, or set it to `free_for_admins`, `free_for_researchers` or `presets_only`. Restart `api` after changing it.
3. **Verify.** As any signed-in user, `GET /api/v1/config` returns `{"deployment_profile": "on-premise", "run_plan_policy": "free_for_researchers", "free_mode_allowed": <true for admin and researcher>}`. If it still says `research-public` or `presets_only`, the `api` container did not get the new `.env` (recreate it with `make up`).

**Warning.** Setting `RUN_PLAN_POLICY` to anything other than `presets_only` while the profile is `research-public` or `mask-only` stops the API at startup, legacy routes included (the whole application fails to import). The Compose default profile is `research-public`, so setting `RUN_PLAN_POLICY=free_for_researchers` without `DEPLOYMENT_PROFILE=on-premise` takes the site down. Fix by setting the profile or unsetting the policy and restarting.

**Invalid values stop the whole application.** Since card F1-21, an unknown `DEPLOYMENT_PROFILE` (not `research-public`, `mask-only` or `on-premise`) or an unknown `RUN_PLAN_POLICY` (not `presets_only`, `free_for_admins` or `free_for_researchers`) makes `api` exit at startup with a `RuntimeError` naming the allowed values; before that release a bad profile only failed the v1 requests that read it. After editing these two variables, check `docker compose logs api` for the start-up error if the stack does not become healthy.

#### Preset files and `PRESETS_DIR`

The API and `seed_implementations` find presets with the same function (`orchestrator.catalog.resolve_presets_dir`): `PRESETS_DIR` when set, else the pipelines directory, scanning every `presets/<id>.yaml`. A preset file must be named after its `id` (`full.yaml` holds `id: full`); a file whose name and `id` differ is a load error, logged by the API and reported (and skipped) by the seeding command. `seed_implementations --reset` also disables the `preset_settings` rows of presets whose file no longer exists and lists them as `update`. The Compose file does not forward `PRESETS_DIR` to `api` yet; with Compose, keep presets under the pipelines tree.

#### What is not available yet

The step images of `stroke-lnm` 0.7.0 are published and pinned: the digests in `pipelines/stroke-lnm/registry.yaml` and `pipeline.yaml` are those of the Docker Hub release runs and the file says `published: true` (see "Publishing step images" below). A step version that is not released yet keeps the placeholder (64 zeros), which the executor never pulls.

**The `deployment` profile is the lab's minimum pipeline** (MVP-01): a lesion mask already in MNI152NLin6Asym space goes through lesion intake and QC, functional disconnection (`flnm-fast`, HCP Aging by default, HCP Young Adult as the alternative) with the COGNET-STROKE cognitive profile, and tract-wise disconnection (`lnm-tracts`), into the research report (English and Spanish). The `deployment` profile enables only those implementations and one preset, `mni-mask-analysis` (default), which contains exactly that. Everything else is shown in the web application greyed and labelled "In development", never selectable: normalisation of CT or MRI lesions to MNI (the `ct-lesion-to-mni152` step is published but its slot is not mounted here, so CT-mode runs are refused; it stays enabled in `ci`), lesion segmentation, the lesion bank and voxel-wise structural LNM (HPC). The list is data, not code: the `in_development` list of `registry.yaml`, served by `GET /api/v1/pipelines/{id}/versions/{version}/in-development` (an entry with `slot` is shown only while that slot has no enabled implementation; an entry without `slot` is a stage that has no step yet). To mount normalisation later, enable it in the `deployment` profile (and the presets that need it); its entry then disappears from the list. The `full`, `ct-normalisation` and `lesion-qc-report` presets exist but are not enabled in `deployment` because they also accept the CT input set.

**Reference bundles.** The bundles the lab installs locally (`docs/reference-data.md`) are pinned by the deployment entries: `normative-fc-hcp-aging` 1.0.0 and `normative-fc-hcp-ya` 1.0.0 (`flnm-fast-hcp-aging`, `flnm-fast-hcp-ya`), `hcp1065-tracts` 1.0.0 (`lnm-tracts-hcp1065`, bundle requirement `tract_atlas`) and `cognet-concept-maps` 1.0.0 (`cognet-stroke-concept-maps`, requirement `concept_maps`). Pins live in the implementation entries of `pipeline.yaml` and the contract has no per-profile pin, so the deployment profile enables entries that are the same step image with a pin (`lnm-tracts-hcp1065`, `cognet-stroke-concept-maps`), while the unpinned entries (`lnm-tracts`, `cognet-stroke`) stay for CI, which registers synthetic bundles and keeps using them. A run on a missing bundle is refused (`bundle_unavailable`).

**Existing instances.** `seed_implementations` never touches rows that exist, so an instance seeded before 0.7.0 keeps its old rows (normalisation enabled, the unpinned tract and COGNET entries, four presets). Re-seed with `python -m app.cli.seed_implementations --profile deployment --reset`, which puts every implementation and preset row back to the profile (it also overwrites any admin change). The display titles and the in-development list need no seeding: the API reads them from `registry.yaml`.

#### Validation levels

Every real implementation in `registry.yaml` carries an explicit `validation.status` (`registry.py check` fails otherwise). All current steps are `reference-tested`: their reference tests run in CI on synthetic or public data, but no validation record signed off by the responsible scientist exists, so none is `validated`. Consequence: the public profiles (`research-public`, `mask-only`; minimum `validated`) cannot enable any current step, and the lab instance runs as `on-premise` (minimum `reference-tested`, results labelled experimental). A step becomes `validated` only when its `validation.record` (a document in the repository, without data) and `validation.reviewed_by` are added by a reviewed pull request.

#### Building a `build: local` wrapper

An external wrapper whose licence forbids publishing its image (registry entry `build: local`) is never built or pushed by CI and never carries a public-registry image reference. Each deployment builds it from the pinned source and the upstream artefact obtained under the licence: `docker build -f <dockerfile of the entry> -t <local tag> .` from the repository root, then read the digest (`docker image inspect <local tag> --format '{{.Id}}'`, or the `RepoDigests` entry of the deployment's own registry), add `"<step name>@<version>": "<repository>@sha256:<digest>"` to the deployment's digests file and run `python pipelines/stroke-lnm/registry.py materialize --digests <file> --out <dir>`. The materialised pipeline and registry are what the API and executor mount; runs record that digest.

#### Publishing step images

The step images are published to Docker Hub as `docker.io/compneurobilbaolab/step-<name>:<step version>` by the workflow `.github/workflows/release-steps.yml` (`release-steps`; logic in `pipelines/stroke-lnm/release.py`). Tags are immutable and deployments always pull by digest, never by tag.

**What the workflow does** for each selected step: builds the image from its pinned source (this repository, or the external repository at the commit pinned in `registry.yaml`, fetched by `registry.py fetch`), checks that `/opt/step/step.yaml` in the image is the step the registry names, runs the step's reference test against that image (contract section 6 flags), and only then pushes that same image to Docker Hub and reads the digest back from the registry. A tag that already exists is never overwritten: it is checked before the build (an existing tag is not rebuilt, status `already-published`, and its published digest is recorded) and again right before the push (an existing tag then stops the release with an error; release a new step version instead). `build: local` entries are never built or pushed. The digests go to the job summary and to the artifact `step-digests` (`published-digests.json`, the format of `registry.py materialize`).

**Running it** (project owner, with write access to the repository):

1. Merge the pull request into `develop` (a real publish only runs from a commit that is on `develop` or `main` of `compneurobilbao/stroke-server-biobizkaia`).
2. Make sure the Docker Hub repositories exist (see below) and that the repository secrets `DOCKERHUB_USERNAME` and `DOCKERHUB_TOKEN` are set.
3. GitHub, Actions, "Release step images", "Run workflow" on `develop`: `steps` = `all` (or step names such as `lesion-qc,research-report`) and `dry_run` checked first. A dry run builds and tests every step and pushes nothing; pull requests that touch the release files do the same automatically. When it is green, run the workflow again with `dry_run` unchecked. Pushing a tag `steps-v<anything>` on a commit of `develop` or `main` publishes all steps (`git tag steps-v1 && git push origin steps-v1`); the tag name is only a label, the image tags are the step versions.
4. Download the artifact `step-digests` (of several runs, merge them: the latest version of each step wins, and a version the registry no longer names, such as a superseded one, is dropped) and pin the digests: `python pipelines/stroke-lnm/registry.py pin --digests published-digests.json`. It rewrites the placeholder image references in `registry.yaml` and `pipeline.yaml` in place (comments are kept), refuses references that are not digest-pinned, are placeholders or point to another repository than the entry's, and sets `published: true` once every implementation the `deployment` profile enables has a real digest. Commit both files in a pull request. From then on `registry.py check` (and CI) fails if the deployment profile references a placeholder. CI itself is unaffected: `registry.py materialize` still replaces the images with the digests of its throwaway registry at run time.

**Docker Hub repositories.** The release pushes seven repositories: `step-lesion-qc`, `step-ct-lesion-to-mni152`, `step-flnm-fast`, `step-fdisconnection-stub`, `step-lnm-tracts`, `step-cognet-stroke` and `step-research-report`. Docker Hub creates a repository on first push for an organisation member with write permission, with the organisation's default visibility; if that does not happen (or private is the default), create them first as **public** repositories in the `compneurobilbaolab` organisation (Docker Hub, Repositories, Create). A release that cannot tell whether a tag exists (authentication or network error) stops instead of pushing. The engine image `compneurobilbaolab/flnm-fast` that the `step-flnm-fast` wrapper builds on is not republished: it stays referenced by its own digest in the Dockerfile.

**Token rotation.** `DOCKERHUB_TOKEN` is a Docker Hub personal access token with read and write permission (Docker Hub, Account settings, Personal access tokens). To rotate it: create a new token, update the repository secret (Settings, Secrets and variables, Actions), run a dry run and then the next release (or a publish of one step), and delete the old token. Revoke the token at once if it may have leaked. The workflow never prints it (the login action masks it); never put it in the repository, in `.env` or in logs.

**Pulling by digest.** A deployment mounts the pinned `pipeline.yaml` and `registry.yaml` (the repository tree once the digests are committed, or the output of `registry.py materialize --digests FILE --out DIR`); the docker executor starts each step as `repository@sha256:...` and pulls it from Docker Hub on first use (no registry login is needed for public images). The step manifests come from the images (`docker run --rm --entrypoint cat <image@digest> /opt/step/step.yaml`). On `on-premise` hosts without outbound access, pull on a connected machine and move the images with `docker save` and `docker load` (the digest is preserved), or mirror them into a local registry with `docker buildx imagetools create --tag <local>/<name>:<version> <repository>@sha256:...` and materialise the pipeline with the mirror's digests.

**Licences.** The platform is non-commercial research software. The image `step-ct-lesion-to-mni152` contains FSL (flirt, fslmaths, cluster), whose licence is non-commercial for the general public; the project owner accepts publishing it on that basis, recorded in the `licence_review` of its registry entry. Anyone who uses the image commercially needs their own FSL licence. The licences of the other components are in the step manifests (`licence`, `licence.third_party`).

The reference path is the CI one: the `e2e-stroke-lnm` workflow (`.github/workflows/e2e-stroke-lnm.yml`, `docs/processing-pipeline.md`) builds the steps, obtains digests from a throwaway registry, materialises the pipeline, registers toy bundles, seeds the `ci` profile and runs the pipeline on synthetic fixtures. Reproduce a deployment the same way by replacing the throwaway digests with the published ones.

### Validation

CI runs `docker compose config` with and without the profile, and `tests/test_deploy_wiring.py` checks the queue routing and the structure of the services. Neither starts containers.

## Release with the legacy cut-over (migration 0012)

Migration `0012_legacy_cutover` moves the legacy studies to the case model (docs/data-model.md 10.1 stage 2): from this release the legacy endpoints and the legacy worker task read and write only the new tables, and `studies` and `processing_jobs` are frozen. A worker of the previous release must not process anything after the migration, and a worker of this release must not start before it. Follow this order, on top of the general deployment protocol (backup first):

1. Check that no study is processing (admin dashboard), then stop the worker(s): `docker compose stop worker` (and `pipeline-worker` if used). Uploads made from now on stay queued.
2. Pull the new images. With the new api image and the database still at revision 0011, run the dry run of the reconciliation: `docker compose run --rm --no-deps api python -m app.cli.legacy_reconcile`. It prints what the migration will insert, update and rewrite, how many legacy artefact sizes it will fill and how many files are missing; it exits with status 1 and lists the ids if legacy rows cannot be mapped. Fix those rows (or `STORAGE_ROOT`) and repeat until it passes.
3. Start the api of the new release (`docker compose up -d api`): its start command runs `alembic upgrade head`, which applies 0012. Check its log for the `legacy reconciliation` and `legacy artefact sizes` lines.
4. Start the worker(s) of the new release. About a minute after start-up each legacy worker runs one recovery pass: attempts queued before the deploy whose message was lost (or consumed by the old worker) are re-enqueued once, and running attempts whose worker died are failed with `legacy_worker_lost`. To see it, or to run it by hand: `docker compose exec worker python -m app.cli.legacy_recover` (report) and `... --execute`.
5. Smoke test and `make e2e`.

Rollback before any new upload: stop the workers, `alembic downgrade 0011_case_model` (writes back to `studies` and `processing_jobs` whatever changed after the cut-over, docs/data-model.md 10.5), then deploy the previous tag. Rows of `audit_events` and `notifications` keep their `case_id` links; the previous release ignores them.

## Basic Production

- Change secrets in `.env`.
- Set `AUTH_SECRET_KEY` to your own value.
- Review all the variables described in `docs/configuration.md`.
- Restrict network access to the server.
- Add TLS in Nginx or Caddy.
- Run `make backup` to back up PostgreSQL and `data/studies`, and copy `backups/` off the server if recovery from disk loss is needed.
- Review the retention policy before using sensitive data.
