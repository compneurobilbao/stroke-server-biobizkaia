# Operations Manual

## Commands

- `make up`: start services.
- `make down`: stop services.
- `make logs`: inspect logs.
- `make smoke`: check the healthcheck.
- `make migrate`: apply Alembic migrations in the `api` container.
- `make create-admin EMAIL=admin@example.org`: create or update the initial admin.
- `make backup`: create a local backup of PostgreSQL and `data/studies`.
- `make restore BACKUP_DIR=backups/<timestamp> CONFIRM_RESTORE=YES_I_UNDERSTAND`: restore a local backup.
- `make frontend-rebuild`: rebuild and recreate only the Docker frontend.
- `make up WORKER_REPLICAS=2`: start services with two `worker` containers.
- `make rebuild`: rebuild and recreate all Docker services; accepts `WORKER_REPLICAS=N`.
- `make clean`: clean local volumes and studies.

## Users

After starting the services, apply migrations before creating the first admin user:

```bash
make up
make migrate
```

Then create or update the initial admin:

```bash
make create-admin EMAIL=admin@example.org
```

The command asks for the password on the console. There is no open public registration; the other users are created from the GUI with an admin account.

## Data And Backups

- PostgreSQL stores metadata, states and audit records.
- `data/studies/{study_id}` stores input, BIDS, results, logs and metadata.
- Back up PostgreSQL and `data/`; both are needed to rebuild traceability.
- Initial retention is run via CLI/configuration and not from the GUI. Real deletion is disabled by default.

Phase 4 implements local backup/restore via CLI, without endpoints or an admin screen:

- `make backup` creates `backups/<timestamp>/db.sql`, `studies.tar.gz` and `manifest.txt`.
- The backup is first written to a temporary `backups/.tmp-*` directory and only published as `backups/<timestamp>` if it finishes successfully.
- `db.sql` is generated with `pg_dump` inside the `postgres` container.
- `studies.tar.gz` archives `data/studies` from an auxiliary container based on `api`, so that results created as `root` by the processor can be read.
- During the backup `api` and `worker` are paused to avoid concurrent writes.
- The backup is aborted if `queued` or `processing` jobs exist; first wait for, cancel or resolve those jobs.
- Archiving `data/studies` uses the list of studies registered in PostgreSQL to avoid backing up orphan folders from interrupted uploads.
- The backup is also aborted if the local folder of an active study is missing; it only tolerates missing folders of already deleted studies.
- Backup files are created with private permissions through `umask 077`.

Study retention is applied explicitly from the `api` container:

- `make retention-dry-run DAYS=90` shows candidates without deleting data.
- `make retention-execute DAYS=90` deletes old terminal studies, records audit events and removes the physical folder only if `RETENTION_ENABLED=true`.
- Only `completed`, `failed` or `canceled` studies with an age greater than or equal to `DAYS` are candidates.
- `queued` and `processing` studies, already deleted studies and studies marked `keep_forever` are excluded.
- Keep `RETENTION_ENABLED=false` except during a controlled operational window; run the dry run first and review candidates before using `retention-execute`.
- `backups/` is ignored by Git; it must be copied off the server if real protection against disk loss is needed.
- `make restore BACKUP_DIR=backups/<timestamp> CONFIRM_RESTORE=YES_I_UNDERSTAND` stops `api` and `worker`, restores PostgreSQL and `data/studies`, starts `api` and `worker` again, waits for `api` to respond internally and restarts `reverse-proxy` to refresh the `api` upstream.
- The restore clears Redis with `FLUSHALL` because the Celery queue is not part of the restorable unit.
- The PostgreSQL restore runs inside a transaction to avoid leaving the database partially replaced if the SQL fails.
- Before replacing `data/studies`, the restore moves the previous folder to `data/pre-restore/studies-<timestamp>`.
- After a restore, the script waits briefly for the proxy and runs `scripts/smoke.sh` with short retries to confirm that the API responds through the proxy.

A useful backup must treat the database and the filesystem as one logical unit. Restoring only PostgreSQL or only `data/studies` can leave studies without files, files without metadata or incomplete audit records.

Redis is not backed up in this phase: the Celery queue is considered transient state. Before restoring, check that there are no critical processing runs in progress.

## Admin Dashboard

The GUI shows an operational dashboard only for `admin` with:

- queue state and failed jobs.
- simultaneous processing capacity detected from Celery.
- PostgreSQL, Redis and Worker healthchecks.
- disk usage and bytes registered in studies.
- users and studies by status.
- basic alerts that do not block processing.

If Redis or Worker appear as `warning` or `down`, check `docker compose ps` and `make logs` before relaunching jobs.

In the GUI, when available, `Cancel` and `Delete` are different actions. A queued or running study must be cancelled first to keep traceability of the attempt; deletion afterwards removes the study and its files. For that reason the `Delete` action is not shown while the study is `queued` or `processing`. The admin panel exposes neither cancel nor retry so that pipelines are not run from administration.

For `compneuro`, do not raise `MAX_CONCURRENT_PROCESSING_JOBS` above `1`. If you need more parallelism, use more replicas with `WORKER_REPLICAS=2` in `.env` or `make up WORKER_REPLICAS=2`. The admin dashboard shows active workers, total capacity and available slots.

To prepare a new compneuro configuration, start from `.env.compneuro.example`: it is commented variable by variable and separates database, queue, processor, concurrency, artefacts, API and SMTP. Do not paste real credentials into versioned files.

## Maintenance

- Semantic versioning is used: `MAJOR.MINOR.PATCH`.
- Update `CHANGELOG.md` on each release.
- Every change to a persistent model requires an Alembic migration.
- If the processor changes, update variables in `.env` and restart `api` and `worker`.

Recommended future retention:

- Initial policy of 90 days for unprotected studies.
- `keep_forever` flag to exclude specific studies.
- Dry-run mode before physical deletion.
- Audit of every automatic or manual deletion.
- Disk usage alert in the admin dashboard before implementing automatic retention and advanced storage policies.

The recommended deletion combines soft delete in the database with controlled physical deletion of input, BIDS, output, PNG, PDF, ZIP and logs, keeping a minimal audit record.

## Logs

- Container logs: `docker compose logs`.
- Processor technical logs: `data/studies/{study_id}/logs/processor.log`.
- Rendered PNGs: `data/studies/{study_id}/output/rendered_png/`.
- Technical PDF: `data/studies/{study_id}/output/reports/technical_report.pdf`.
- Results ZIP: `data/studies/{study_id}/output/outputs.zip`.
- Compneuro outputs: `data/studies/{study_id}/output/Preproc`.
- Rendering log: `data/studies/{study_id}/logs/rendering.log`.

## Incidents

First check the state in the GUI, then the worker logs, then `processor.log`. For `compneuro`, also check that `output/Preproc/BET`, `output/Preproc/ProbTissue`, `output/rendered_png/` and `logs/rendering.log` exist.

## Retry And Rerun

Retrying failed studies is exposed in the GUI and creates a new `ProcessingJob`, keeping traceability of the previous attempt.

If re-run manually outside the GUI, keep previous results in a versioned or timestamped folder before launching the task again.

## Future Job Operations

- The current cancellation covers queued jobs and running processing. When running, the API requests termination of the Celery task with `SIGTERM` and the worker/adapter try to terminate the process group of the external command.
- Retrying failed jobs creates a new `ProcessingJob` and keeps traceability of the previous attempt.
- The logs visible in the GUI are returned truncated from `processor.log` and `rendering.log`.
- Deletion applies soft delete in the DB and physical deletion of the study folder; it keeps a minimal audit record.
- The admin dashboard already shows queue, active/failed jobs, disk usage, healthchecks, worker, Redis, PostgreSQL, users and studies by status.
