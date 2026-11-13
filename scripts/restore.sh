#!/usr/bin/env sh
set -eu
umask 077

BACKUP_DIR="${BACKUP_DIR:-}"
CONFIRM_RESTORE="${CONFIRM_RESTORE:-}"
DATA_DIR="${DATA_DIR:-data}"
STUDIES_DIR="${STUDIES_DIR:-${DATA_DIR}/studies}"
PRE_RESTORE_ROOT="${PRE_RESTORE_ROOT:-${DATA_DIR}/pre-restore}"
restore_completed=0
services_stopped=0

wait_for_api_health() {
  attempts=12
  delay_seconds=5
  attempt=1

  while [ "${attempt}" -le "${attempts}" ]; do
    if docker compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=5)" >/dev/null 2>&1; then
      return 0
    fi
    if [ "${attempt}" -lt "${attempts}" ]; then
      printf 'API is not ready yet. Retrying in %s seconds (%s/%s).\n' "${delay_seconds}" "${attempt}" "${attempts}" >&2
      sleep "${delay_seconds}"
    fi
    attempt=$((attempt + 1))
  done

  return 1
}

run_smoke_with_retries() {
  attempts=5
  delay_seconds=3
  attempt=1

  while [ "${attempt}" -le "${attempts}" ]; do
    if ./scripts/smoke.sh; then
      return 0
    fi
    if [ "${attempt}" -lt "${attempts}" ]; then
      printf 'Smoke test failed after the restore. Retrying in %s seconds (%s/%s).\n' "${delay_seconds}" "${attempt}" "${attempts}" >&2
      sleep "${delay_seconds}"
    fi
    attempt=$((attempt + 1))
  done

  return 1
}

cleanup() {
  if [ "${services_stopped}" -eq 1 ] && [ "${restore_completed}" -ne 1 ]; then
    docker compose stop api worker >/dev/null 2>&1 || true
    printf 'Restore interrupted or failed. api and worker stay stopped to avoid writes on a potentially partial state.\n' >&2
  fi
}

trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

if [ -z "${BACKUP_DIR}" ]; then
  printf 'Usage: BACKUP_DIR=backups/<timestamp> CONFIRM_RESTORE=YES_I_UNDERSTAND sh ./scripts/restore.sh\n' >&2
  exit 1
fi

if [ "${CONFIRM_RESTORE}" != "YES_I_UNDERSTAND" ]; then
  printf 'Restore blocked. Re-run with CONFIRM_RESTORE=YES_I_UNDERSTAND.\n' >&2
  exit 1
fi

if [ ! -d "${BACKUP_DIR}" ]; then
  printf 'Backup directory does not exist: %s\n' "${BACKUP_DIR}" >&2
  exit 1
fi

if [ ! -r "${BACKUP_DIR}" ] || [ ! -x "${BACKUP_DIR}" ]; then
  printf 'Cannot read the backup directory: %s\n' "${BACKUP_DIR}" >&2
  printf 'Check ownership and permissions. If it was created with sudo, do not restore it as a normal user without fixing permissions.\n' >&2
  exit 1
fi

if [ ! -f "${BACKUP_DIR}/db.sql" ] || [ ! -f "${BACKUP_DIR}/studies.tar.gz" ] || [ ! -f "${BACKUP_DIR}/manifest.txt" ]; then
  printf 'Incomplete backup: db.sql, studies.tar.gz and manifest.txt are required.\n' >&2
  exit 1
fi

tar -tzf "${BACKUP_DIR}/studies.tar.gz" >/dev/null

backup_commit="$(sed -n 's/^git_commit=//p' "${BACKUP_DIR}/manifest.txt" | sed -n '1p')"
current_commit="$(git rev-parse --short HEAD 2>/dev/null || printf 'unknown')"

if [ -n "${backup_commit}" ] && [ "${backup_commit}" != "unknown" ] && [ "${current_commit}" != "unknown" ] && [ "${backup_commit}" != "${current_commit}" ]; then
  printf 'Warning: the backup was created at git_commit=%s and the current checkout is %s.\n' "${backup_commit}" "${current_commit}" >&2
fi

timestamp="$(date +%Y%m%d-%H%M%S)"
pre_restore_dir="${PRE_RESTORE_ROOT}/studies-${timestamp}"

printf 'Stopping api and worker to avoid writes during the restore.\n'
docker compose stop api worker
services_stopped=1

printf 'Making sure PostgreSQL and Redis are running.\n'
docker compose up -d postgres redis

printf 'Restoring PostgreSQL from %s/db.sql\n' "${BACKUP_DIR}"
{
  printf 'BEGIN;\n'
  printf 'DROP SCHEMA IF EXISTS public CASCADE;\n'
  printf 'CREATE SCHEMA public;\n'
  cat "${BACKUP_DIR}/db.sql"
  printf '\nCOMMIT;\n'
} | docker compose exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'

printf 'Flushing Redis because the Celery queue is transient state and is not restored.\n'
docker compose exec -T redis redis-cli FLUSHALL >/dev/null

if [ -d "${STUDIES_DIR}" ]; then
  mkdir -p "${PRE_RESTORE_ROOT}"
  printf 'Moving %s to %s\n' "${STUDIES_DIR}" "${pre_restore_dir}"
  mv "${STUDIES_DIR}" "${pre_restore_dir}"
fi

mkdir -p "${DATA_DIR}"
printf 'Restoring data/studies from %s/studies.tar.gz\n' "${BACKUP_DIR}"
tar -xzf "${BACKUP_DIR}/studies.tar.gz" -C "${DATA_DIR}"

printf 'Restarting api and worker.\n'
docker compose up -d api worker

printf 'Waiting for api to respond internally.\n'
wait_for_api_health

printf 'Restarting reverse-proxy to refresh the api upstream.\n'
docker compose restart reverse-proxy

sleep 3

printf 'Verifying API through the reverse proxy.\n'
run_smoke_with_retries

restore_completed=1
printf 'Restore completed and verified.\n'
