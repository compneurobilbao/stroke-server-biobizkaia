# Backup and restore of a deployment

This page covers `scripts/ops/backup.sh` and `scripts/ops/restore.sh` (`make ops-backup`, `make ops-restore`). They back up a Docker Compose deployment as one unit: the PostgreSQL database and the data tree that its rows point to. CI runs backup -> wipe -> restore on a throwaway stack in `.github/workflows/backup-restore.yml` (`scripts/ops/test_backup_restore.sh`).

The older `make backup` / `make restore` (`scripts/backup.sh`, `scripts/restore.sh`, docs/operations-manual.md) remain for in-place rollback of the legacy study model. They pause `api` and `worker`, archive only the folders of rows in the frozen `studies` table, and do not cover cases, pipeline runs or bundles. Use the scripts on this page for deployments that run the case model and pipeline runs.

## What is backed up

A backup is a directory `<dest>/stroke-server-backup-<UTC timestamp>/` with:

| File | Content |
|---|---|
| `db.dump` | `pg_dump -Fc` of the application database: users, cases, input files, pipeline runs, slots, step runs, artifacts, reports, audit events, notifications, share links, implementation and preset settings, the reference bundle registry rows (`reference_bundles`), the legacy tables and `alembic_version`. |
| `data.tar.gz` | The data directory (`./data`, mounted as `/app/data`): `studies/<case or study id>/` with inputs, run work directories (`work/<run id>/`), outputs, reports, logs and metadata, plus the bundle registry file `bundles/.registry.json` when the JSON store is used. Owners (numeric) and modes are kept. |
| `bundles.tar.gz` | Only with `--include-bundles`: the whole bundle root (`data/bundles/<id>/<version>/`). |
| `bundles.tsv` | Inventory of the registered bundles: id, version, storage path, content digest, manifest SHA-256, status, redistributable flag. |
| `manifest.json` | Format version, creation time, Compose project, git commit (and whether the checkout was modified), alembic revision, PostgreSQL server version, `PLATFORM_VERSION` of the running api, the pipelines of the checkout with their version and registry version, run counts per pipeline version, case and run counts, and size and SHA-256 of every file. |
| `SHA256SUMS` | The same checksums in `sha256sum -c` format. |

The dump is taken with `docker compose exec postgres pg_dump`, using the `POSTGRES_USER` and `POSTGRES_DB` of the container's own environment. The script never reads, prints or stores the database password, and the backup name contains only a timestamp.

### What is not backed up

- **Reference bundle payloads**, by default. They are large, immutable and re-creatable from their sources: each version is identified by its content digest and manifest SHA-256 (in `bundles.tsv`), so a copy put back at `data/bundles/<id>/<version>/` is checked by `python -m orchestrator.bundles --store db verify` after a restore. Pass `--include-bundles` when the original sources are not kept anywhere else.
- **Redis.** The Celery queue is transient state. Runs are recorded in PostgreSQL and `recover_pipeline_runs` (the `beat` service) resumes or fails runs whose task was lost. Legacy jobs that were queued at backup time need the handling in docs/operations-manual.md ("Legacy studies left without a task").
- **`data/pre-restore/`**, the safety copies left by earlier restores.
- **Configuration and secrets**: `.env`, TLS certificates, the reverse-proxy configuration and any Compose overrides that are not in the repository. Keep them in the deployment's secret store. A restore needs the same `AUTH_SECRET_KEY` for existing tokens and share links to stay valid. Users can log in again with a new key.
- **Container images.** Step images are pinned by digest in the pipeline and pulled again. Application images are rebuilt from the git commit in the manifest.
- **Databases outside the Compose `postgres` service.** If `DATABASE_URL` points elsewhere, back that database up with its own tooling.

## Data-use terms and off-site copies

Bundles derived from third-party data under data-use terms (for example connectomes or tract atlases derived from Human Connectome Project data) stay on the server where the terms allow them to be. That is the main reason why bundle payloads are left out of the default backup. Before an archive made with `--include-bundles` leaves the server, check the `redistributable` column of `bundles.tsv` and the licence of each bundle. Do not copy such archives to storage that the terms do not cover.

The default backup also contains case data (inputs, outputs, reports). It must be stored and transferred with the same protection as the server itself: private permissions (the scripts use `umask 077`), encrypted transport, and encryption at rest off-site (for example `gpg --symmetric` or an encrypted backup repository such as restic or borg). Keep the off-site copy under the same retention rules as the deployment.

## Running a backup

```sh
make ops-backup                                     # into ./backups, keep everything
make ops-backup ARGS="--dest /backup/stroke --keep 14"
make ops-backup ARGS="--include-bundles --dest /backup/stroke-full --keep 2"
scripts/ops/backup.sh --help
```

Options: `-p/--project-name` (Compose project), `-C/--compose-dir` (directory with `docker-compose.yml`, default the repository root), `-d/--data-dir` (host directory mounted as `/app/data`, default `<compose-dir>/data`), `-o/--dest` (default `<compose-dir>/backups`), `-k/--keep N` (keep the newest N complete backups, default 0 = all), `--include-bundles`, `--bundles-subdir` (default `bundles`).

Behaviour:

- `set -euo pipefail`. The backup is written to `<dest>/.tmp-*` and only renamed to its final name when complete, so an interrupted run leaves no partial backup. A lock (`flock`, when available) prevents two backups writing to the same destination at once.
- The script refuses a destination inside the data directory, because a backup must not be archived by the next backup.
- The application keeps running. `pg_dump` is a consistent snapshot. The data tree is archived after the dump, so every file the dump refers to already exists when it is read. Files written after the dump are included as orphans, which is harmless. Work directories of runs still in progress may be partial. After a restore, such runs are in an active state, and recovery resumes or fails them like after a crash. Files that change while they are read produce a warning, not a failure. For a fully quiescent backup, schedule it when no run is active or stop `pipeline-worker` and `worker` around it.
- Files written by the containers usually belong to other users. When the script does not run as root, it archives and extracts the data inside a throwaway container of the `api` image, with the data directory mounted read-only. As root it uses the host's `tar`. Retention only removes directories named `stroke-server-backup-*` that contain a `manifest.json`.

## Scheduling

A cron entry, as a user in the `docker` group (or root), from the deployment checkout:

```cron
# m h dom mon dow
15 2 * * *  cd /path/to/deployment && scripts/ops/backup.sh --dest /backup/stroke --keep 14 >> /var/log/stroke-backup.log 2>&1
30 3 * * 0  cd /path/to/deployment && scripts/ops/backup.sh --include-bundles --dest /backup/stroke-full --keep 2 >> /var/log/stroke-backup.log 2>&1
```

Or a systemd service and timer:

```ini
# /etc/systemd/system/stroke-backup.service
[Unit]
Description=Backup of the stroke-server deployment
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
WorkingDirectory=/path/to/deployment
ExecStart=/path/to/deployment/scripts/ops/backup.sh --dest /backup/stroke --keep 14
Nice=10
IOSchedulingClass=idle
```

```ini
# /etc/systemd/system/stroke-backup.timer
[Unit]
Description=Nightly backup of the stroke-server deployment

[Timer]
OnCalendar=*-*-* 02:15:00
RandomizedDelaySec=15m
Persistent=true

[Install]
WantedBy=timers.target
```

Enable the timer with `systemctl enable --now stroke-backup.timer`, and read the logs with `journalctl -u stroke-backup.service`. Add `OnFailure=` to a notification unit, or monitor the age of the newest backup directory, so a failing backup is noticed. Copy the finished backups off the server with a separate job (rsync, restic, borg). Do not write the backups directly to a network mount, because the dump would then depend on that mount being available.

## Restoring

`restore.sh` restores into a stopped, empty deployment, for example a new server or the same server after the volumes were lost:

```sh
git checkout <git_commit from manifest.json>      # or any later commit; see below
docker compose stop api worker pipeline-worker beat  # if the stack exists
make ops-restore BACKUP=/backup/stroke/stroke-server-backup-20260101T021500Z
# bundles not in the backup: put them back at data/bundles/<id>/<version>/ (list: bundles.tsv)
docker compose up -d                                  # api runs the migrations on start
docker compose exec api python -m orchestrator.bundles --store db --root /app/data/bundles verify
make smoke
```

What it checks and does, in order:

1. Every file against `SHA256SUMS`, and `SHA256SUMS` against `manifest.json`. A damaged backup is refused before anything is touched.
2. The alembic revision of the backup against the migrations of the checkout:
   - **Same revision:** the restore goes ahead.
   - **Older, known revision:** the restore goes ahead and the script tells you that the migrations run on start (or with `make migrate`).
   - **Unknown revision:** the backup comes from newer code, so the restore is refused. Check out the commit in the manifest. `--allow-unknown-revision` overrides this.
3. The script refuses while `api`, `worker`, `pipeline-worker` or `beat` run. It reads Compose container labels, so this also covers services of profiles that are not enabled in the shell.
4. It starts `postgres` and refuses if any table other than `alembic_version` holds rows. `--force` replaces such a database. An empty, migrated schema (from starting the stack before the restore) is replaced without `--force`.
5. `pg_restore --single-transaction --exit-on-error`.
6. It refuses if the data directory holds files outside the bundle root, other than the repository's `README.md` and `studies/.gitkeep`. With `--force`, that data is moved to `data/pre-restore/<timestamp>/` and never deleted. Remove it yourself once the restore is verified.
7. It extracts `data.tar.gz` (and `bundles.tar.gz` if present) with numeric owners and modes preserved.
8. It empties Redis if it is running.

The script does not start the application. If the database restore succeeds and the file restore fails, the database stays restored and the script says so. Fix the cause and re-run with `--force`.

## Testing a restore

A backup that has never been restored is untested. At least once per release, and after changes to the storage layout:

1. Copy a recent backup to a test machine or a separate checkout, using a different Compose project name (`-p`) and data directory, so the production stack is untouched.
2. Run `scripts/ops/restore.sh -p restore-test -C <checkout> <backup>`, start the stack, and run the bundle `verify` and `make smoke`.
3. Open a few cases and a finished run in the GUI, and download a report.
4. Write down the time each step took. That time is your measured RTO.
5. Remove the test stack (`docker compose -p restore-test down -v`) and its data directory.

CI covers the mechanics on every change to `scripts/ops/`, `backend/` or the Compose files. It seeds a case, a succeeded run, a run output file (with a non-default owner and mode) and a registered bundle. It then backs up while the api runs and checks the refusals: a destination inside the data directory, a running api, a damaged backup, and a database with data. It then restores after losing the database and data with the bundles left in place (default backup), after losing everything (`--include-bundles`), and with `--force` over a populated deployment. Each time it checks that the case, the run, the file content, owner and mode, and the bundle checksums survived, and that the database password appears in neither the backup nor the output.

## RPO and RTO

- **RPO** (data that can be lost) is the backup interval: with the nightly schedule above, up to one day of uploads and runs. Runs can be repeated from their inputs if those inputs were uploaded before the last backup. Run the backup more often (for example every 4 hours) if cases are added during the day. The cost is one `pg_dump` and one pass over the data tree. For a smaller RPO on the database, use PostgreSQL WAL archiving or a replica. This tooling does not set that up.
- **RTO** (time to service) is roughly provisioning plus image build or pull, plus restore, plus bundle copy. The restore is about the time to read the archives. Bundles not in the backup must be copied back from their source before runs that need them can start. Cases, reports and finished runs are available as soon as the api is up. Measure the RTO with the restore test above instead of estimating it.
- Off-site copies set the RPO for a lost server. A backup that exists only on the server's own disk does not protect against disk loss.
