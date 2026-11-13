#!/usr/bin/env sh
set -eu
umask 077

BACKUP_ROOT="${BACKUP_ROOT:-backups}"
DATA_DIR="${DATA_DIR:-data}"
STUDIES_DIR="${STUDIES_DIR:-${DATA_DIR}/studies}"
paused_services=0
backup_completed=0
host_uid="$(id -u)"
host_gid="$(id -g)"
project_dir="$(pwd)"

timestamp="$(date +%Y%m%d-%H%M%S)"
backup_dir="${BACKUP_ROOT}/.tmp-${timestamp}-$$"
final_backup_dir="${BACKUP_ROOT}/${timestamp}"

cleanup() {
  if [ "${paused_services}" -eq 1 ]; then
    docker compose unpause api worker >/dev/null 2>&1 || true
  fi
  if [ "${backup_completed}" -ne 1 ] && [ -d "${backup_dir}" ]; then
    rm -rf "${backup_dir}"
  fi
}

trap cleanup EXIT
trap 'cleanup; exit 130' INT TERM

if [ ! -d "${STUDIES_DIR}" ]; then
  printf '%s does not exist\n' "${STUDIES_DIR}" >&2
  exit 1
fi

if [ -e "${final_backup_dir}" ]; then
  printf 'Cannot create the backup: %s already exists\n' "${final_backup_dir}" >&2
  exit 1
fi

mkdir -p "${backup_dir}"

printf 'Pausing api and worker to avoid writes during the backup.\n'
docker compose pause api worker
paused_services=1

active_jobs="$(docker compose exec -T postgres sh -c "psql -At -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -c \"select count(*) from processing_jobs where status in ('queued','processing');\"")"

if [ "${active_jobs}" != "0" ]; then
  printf 'Backup canceled: there are %s queued/processing jobs. Wait for them to finish or resolve them before backing up.\n' "${active_jobs}" >&2
  exit 1
fi

docker compose exec -T postgres sh -c "psql -At -U \"\$POSTGRES_USER\" -d \"\$POSTGRES_DB\" -c \"select id::text || '|' || coalesce(deleted_at::text, '') from studies order by id;\"" > "${backup_dir}/studies-db-rows.txt"

tar_paths="${backup_dir}/studies-paths.txt"
: > "${tar_paths}"
: > "${backup_dir}/studies.ids"
missing_active_studies=0

if [ -f "${STUDIES_DIR}/.gitkeep" ]; then
  printf 'studies/.gitkeep\n' >> "${tar_paths}"
fi

while IFS= read -r study_row; do
  study_id="${study_row%%|*}"
  deleted_at="${study_row#*|}"
  if [ -z "${study_id}" ]; then
    continue
  fi
  printf '%s\n' "${study_id}" >> "${backup_dir}/studies.ids"
  if [ -d "${STUDIES_DIR}/${study_id}" ]; then
    printf 'studies/%s\n' "${study_id}" >> "${tar_paths}"
  elif [ -n "${deleted_at}" ]; then
    printf 'Warning: deleted study without a local folder for study_id=%s\n' "${study_id}" >&2
  else
    printf 'Error: no local folder for active study_id=%s\n' "${study_id}" >&2
    missing_active_studies=1
  fi
done < "${backup_dir}/studies-db-rows.txt"

if [ "${missing_active_studies}" -ne 0 ]; then
  printf 'Backup canceled: local folders of active studies are missing.\n' >&2
  exit 1
fi

printf 'Creating PostgreSQL backup at %s/db.sql\n' "${backup_dir}"
docker compose exec -T postgres sh -c 'pg_dump --clean --if-exists --no-owner --no-privileges -U "$POSTGRES_USER" "$POSTGRES_DB"' > "${backup_dir}/db.sql"

printf 'Creating backup of %s at %s/studies.tar.gz\n' "${STUDIES_DIR}" "${backup_dir}"
if [ -s "${tar_paths}" ]; then
  docker compose run --rm --no-deps -T \
    -v "${project_dir}/${backup_dir}:/backup-out" \
    --entrypoint sh \
    api \
    -c "tar -czf /backup-out/studies.tar.gz -C /app/data -T /backup-out/studies-paths.txt && chown ${host_uid}:${host_gid} /backup-out/studies.tar.gz && chmod 600 /backup-out/studies.tar.gz"
else
  empty_root="${backup_dir}/empty-studies"
  mkdir -p "${empty_root}/studies"
  tar -czf "${backup_dir}/studies.tar.gz" -C "${empty_root}" studies
fi

{
  printf 'created_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'git_commit=%s\n' "$(git rev-parse --short HEAD 2>/dev/null || printf 'unknown')"
  printf 'database_dump=db.sql\n'
  printf 'studies_archive=studies.tar.gz\n'
  printf 'studies_ids=studies.ids\n'
  printf 'restore_note=Restore PostgreSQL and data/studies together to keep traceability.\n'
} > "${backup_dir}/manifest.txt"

mv "${backup_dir}" "${final_backup_dir}"
backup_completed=1

printf 'Backup created at %s\n' "${final_backup_dir}"
