# Scripts

This folder contains small operation and validation scripts.

## Responsibility

- Make the repetitive commands used by the `Makefile` easier.
- Avoid error-prone manual instructions.
- Prepare basic checks for development and pre-commit.

## Scripts

- `start.sh`: starts services with `docker compose up -d`.
- `stop.sh`: stops services with `docker compose down`.
- `smoke.sh`: checks the healthcheck through the reverse proxy.
- `check-docs.sh`: validates that the mandatory minimum documentation exists.
- `check-no-secrets.sh`: blocks committed `.env*` files (except `*.example` templates), study data and simple secret patterns; runs `gitleaks` if installed.
- `backup.sh`: creates a local backup of PostgreSQL and `data/studies`; pauses `api`/`worker`, refuses to run with active jobs and archives only studies registered in the DB.
- `restore.sh`: restores PostgreSQL and `data/studies` from a local backup with explicit confirmation, clearing Redis as transient state.
- `seed.sh`: creates a local dummy file for manual tests.

## Relationship With The Makefile

The `Makefile` calls these scripts through commands such as `make smoke`, `make backup`, `make restore`, `make check-docs`, `make check-secrets` and `make seed`.

## Criteria

Scripts must remain simple, readable and safe. They must not delete real data unless the command clearly says so. The restore must keep a local copy of the previous state before replacing `data/studies`.
