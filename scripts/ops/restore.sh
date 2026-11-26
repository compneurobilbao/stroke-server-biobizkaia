#!/usr/bin/env bash
# Restore a backup written by scripts/ops/backup.sh into a stopped, empty Compose deployment
# (card OPS-01, docs/operations/backup-restore.md).
#
#   scripts/ops/restore.sh [options] BACKUP_DIR
#
# Order of work:
#   1. checks every file against SHA256SUMS and manifest.json;
#   2. compares the alembic revision of the backup with the migrations of this checkout: the same
#      revision is fine, an older one is restored and you are told to run the migrations, an
#      unknown (newer) one is refused: check out the commit recorded in the manifest first;
#   3. refuses while api, worker, pipeline-worker or beat are running;
#   4. starts postgres if needed and refuses when its database already holds data (any non-empty
#      table besides alembic_version) unless --force; an empty, migrated schema is replaced;
#   5. pg_restore of db.dump in a single transaction;
#   6. refuses when the data directory already holds data outside the bundle root unless --force,
#      in which case that data is moved to <data>/pre-restore/<timestamp>/ (never deleted);
#   7. extracts data.tar.gz (and bundles.tar.gz when the backup has it) with numeric owners and
#      modes preserved;
#   8. empties Redis when it is running (the Celery queue is transient state, not restored).
# It does not start the application; it prints the commands that do.
#
# Options:
#   -p, --project-name NAME   Compose project name (default: Compose's own default)
#   -C, --compose-dir DIR     directory with docker-compose.yml (default: repository root)
#   -d, --data-dir DIR        host directory mounted as /app/data (default: <compose-dir>/data)
#       --force               restore over a database or data directory that holds data
#       --allow-unknown-revision  restore a backup whose alembic revision this checkout does not know
#   -h, --help
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
COMPOSE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
PROJECT_NAME=""
DATA_DIR=""
FORCE=0
ALLOW_UNKNOWN=0
BACKUP=""
APP_SERVICES="api worker pipeline-worker beat"

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; }
die() { printf 'restore: %s\n' "$*" >&2; exit 1; }
log() { printf 'restore: %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    -p|--project-name) PROJECT_NAME="${2:?}"; shift 2 ;;
    -C|--compose-dir) COMPOSE_DIR="${2:?}"; shift 2 ;;
    -d|--data-dir) DATA_DIR="${2:?}"; shift 2 ;;
    --force) FORCE=1; shift ;;
    --allow-unknown-revision) ALLOW_UNKNOWN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) die "unknown option: $1 (see --help)" ;;
    *) [ -z "$BACKUP" ] || die "only one backup directory"; BACKUP="$1"; shift ;;
  esac
done

[ -n "$BACKUP" ] || { usage >&2; exit 2; }
[ -d "$BACKUP" ] || die "backup directory does not exist: $BACKUP"
BACKUP="$(cd "$BACKUP" && pwd -P)"
[ -f "$COMPOSE_DIR/docker-compose.yml" ] || die "no docker-compose.yml in $COMPOSE_DIR"
COMPOSE_DIR="$(cd "$COMPOSE_DIR" && pwd -P)"
DATA_DIR="${DATA_DIR:-$COMPOSE_DIR/data}"
mkdir -p "$DATA_DIR"
DATA_DIR="$(cd "$DATA_DIR" && pwd -P)"
case "$BACKUP/" in "$DATA_DIR"/*) die "the backup must not live inside the data directory" ;; esac

compose() {
  if [ -n "$PROJECT_NAME" ]; then
    docker compose --project-directory "$COMPOSE_DIR" -p "$PROJECT_NAME" "$@"
  else
    docker compose --project-directory "$COMPOSE_DIR" "$@"
  fi
}
# Credentials stay inside the postgres container (expanded by its shell from its environment).
psql_at() {
  compose exec -T postgres sh -c 'psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<<"$1"
}
manifest_get() { sed -n "s/^  \"$1\": \"\{0,1\}\([^\",]*\)\"\{0,1\},\{0,1\}\$/\1/p" "$BACKUP/manifest.json" | head -n1; }

# Runs a snippet with SRC = the data directory (read-write) and BK = the backup directory
# (read-only): on the host as root, otherwise in a throwaway api container (root inside), so that
# numeric owners and modes can be restored.
in_data_env() {
  local script="$1"
  if [ "$(id -u)" -eq 0 ]; then
    SRC="$DATA_DIR" BK="$BACKUP" bash -c "$script"
  else
    compose run --rm --no-deps -T --entrypoint bash \
      -v "$DATA_DIR:/restore-dst" -v "$BACKUP:/restore-src:ro" \
      -e SRC=/restore-dst -e BK=/restore-src \
      api -c "$script"
  fi
}

# ---------------------------------------------------------------- 1. integrity
[ -f "$BACKUP/manifest.json" ] && [ -f "$BACKUP/SHA256SUMS" ] || die "manifest.json or SHA256SUMS missing in $BACKUP"
log "checking the checksums of $BACKUP"
if command -v sha256sum >/dev/null 2>&1; then
  (cd "$BACKUP" && sha256sum --quiet -c SHA256SUMS) || die "checksum mismatch: the backup is damaged"
else
  (cd "$BACKUP" && shasum -a 256 --quiet -c SHA256SUMS) || die "checksum mismatch: the backup is damaged"
fi
for f in db.dump data.tar.gz; do
  grep -q "  $f\$" "$BACKUP/SHA256SUMS" || die "$f is not listed in SHA256SUMS"
done
# manifest.json and SHA256SUMS must agree.
while read -r sum name; do
  grep -q "\"name\": \"$name\", \"size\": [0-9]*, \"sha256\": \"$sum\"" "$BACKUP/manifest.json" \
    || die "manifest.json and SHA256SUMS disagree about $name"
done <"$BACKUP/SHA256SUMS"
bundles_included="$(manifest_get bundles_included)"
bundles_subdir="$(manifest_get bundles_subdir)"; bundles_subdir="${bundles_subdir:-bundles}"
case "$bundles_subdir" in */*|*..*) die "invalid bundles_subdir in the manifest" ;; esac

# ---------------------------------------------------------------- 2. alembic revision
backup_rev="$(manifest_get alembic_revision)"
versions_dir="$COMPOSE_DIR/backend/alembic/versions"
known_revs="$(sed -n 's/^revision = "\(.*\)"$/\1/p' "$versions_dir"/*.py 2>/dev/null | sort)"
down_revs="$(sed -n 's/^down_revision = "\(.*\)"$/\1/p' "$versions_dir"/*.py 2>/dev/null | sort)"
code_head="$(comm -23 <(printf '%s\n' "$known_revs") <(printf '%s\n' "$down_revs") | tr '\n' ' ' | sed 's/ $//')"
needs_migration=0
if [ -z "$backup_rev" ]; then
  die "the manifest records no alembic revision; this is not a backup of a migrated database"
elif [ "$backup_rev" = "$code_head" ]; then
  log "alembic revision $backup_rev matches this checkout"
elif printf '%s\n' "$known_revs" | grep -qx "$backup_rev"; then
  needs_migration=1
  log "the backup is at alembic revision $backup_rev, this checkout's head is $code_head: the migrations run after the restore"
elif [ "$ALLOW_UNKNOWN" -eq 1 ]; then
  log "warning: alembic revision $backup_rev is unknown to this checkout (head $code_head); continuing (--allow-unknown-revision)"
else
  die "alembic revision $backup_rev is unknown to this checkout (head $code_head): the backup comes from newer code. Check out git commit $(manifest_get git_commit) and restore from there"
fi

# ---------------------------------------------------------------- 3. stopped deployment
# Through the container labels, so services of profiles that are not enabled here count too.
project="$(compose config 2>/dev/null | sed -n 's/^name: //p' | head -n1)"
[ -n "$project" ] || die "cannot resolve the Compose project name"
running="$(docker ps --filter "label=com.docker.compose.project=$project" \
  --format '{{.Label "com.docker.compose.service"}}' | sort -u)"
for svc in $APP_SERVICES; do
  if printf '%s\n' "$running" | grep -qx "$svc"; then
    die "$svc is running; stop the application first: docker compose stop $APP_SERVICES"
  fi
done

# ---------------------------------------------------------------- 4. empty database
log "starting postgres"
compose up -d postgres >/dev/null
for _ in $(seq 1 60); do
  compose exec -T postgres sh -c 'pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' && break
  sleep 2
done
compose exec -T postgres sh -c 'pg_isready -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' || die "postgres did not become ready"

tables="$(psql_at "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")"
nonempty="$(psql_at "SELECT count(*) FROM information_schema.tables t
  WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE' AND t.table_name <> 'alembic_version'
  AND (xpath('/row/n/text()', query_to_xml(format('SELECT count(*) AS n FROM (SELECT 1 FROM %I.%I LIMIT 1) s',
       t.table_schema, t.table_name), false, true, '')))[1]::text::int > 0")"
if [ "$nonempty" -gt 0 ]; then
  [ "$FORCE" -eq 1 ] || die "the database already holds data ($nonempty non-empty tables); restore into an empty deployment or pass --force to replace it"
  log "--force: replacing a database that holds data ($nonempty non-empty tables)"
fi
if [ "$tables" -gt 0 ]; then
  log "dropping the existing public schema ($tables tables)"
  psql_at 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;' >/dev/null
fi

# ---------------------------------------------------------------- 5. database
log "restoring the database from db.dump"
compose exec -T postgres sh -c 'pg_restore --exit-on-error --single-transaction --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  <"$BACKUP/db.dump"
restored_rev="$(psql_at 'SELECT version_num FROM alembic_version' | head -n1)"
[ "$restored_rev" = "$backup_rev" ] || die "restored alembic revision $restored_rev differs from the manifest ($backup_rev)"

# ---------------------------------------------------------------- 6-7. files
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
log "restoring the data tree into $DATA_DIR"
files_script='
set -euo pipefail
cd "$SRC"
B="'"$bundles_subdir"'"
# Data outside the bundle root and earlier pre-restore copies, ignoring the files that the
# repository itself tracks there (README.md, studies/.gitkeep) and empty directories.
existing="$(find . -mindepth 1 \( -path "./$B" -o -path "./pre-restore" \) -prune -o -type f \
  ! -path ./README.md ! -path ./studies/.gitkeep -print | head -n 5)"
if [ -n "$existing" ]; then
  if [ '"$FORCE"' -ne 1 ]; then
    echo "restore: the data directory already holds data, for example:" >&2
    printf "%s\n" "$existing" >&2
    echo "restore: restore into an empty data directory or pass --force (it moves that data to pre-restore/)" >&2
    exit 3
  fi
  aside="pre-restore/'"$stamp"'"
  mkdir -p "$aside"
  find . -mindepth 1 -maxdepth 1 ! -name "$B" ! -name pre-restore -exec mv -t "$aside" {} +
  echo "restore: moved the previous data to $aside"
fi
tar --extract --gzip --same-owner --same-permissions --numeric-owner -f "$BK/data.tar.gz"
if [ -f "$BK/bundles.tar.gz" ]; then
  tar --extract --gzip --same-owner --same-permissions --numeric-owner -f "$BK/bundles.tar.gz"
fi
'
in_data_env "$files_script" || die "restoring the data tree failed (the database is already restored)"

# ---------------------------------------------------------------- 8. redis
if compose ps --services --status running 2>/dev/null | grep -qx redis; then
  log "emptying Redis (transient queue state)"
  compose exec -T redis redis-cli FLUSHALL >/dev/null
fi

log "restore finished from $(manifest_get name)"
if [ "$bundles_included" != true ]; then
  log "reference bundle payloads were not in this backup; the registry expects them under $DATA_DIR/$bundles_subdir/<id>/<version>/ (list: $BACKUP/bundles.tsv)"
fi
cat <<EOF
Next steps:
  1. start the application:  docker compose up -d   (with your usual profiles and options)
$( [ "$needs_migration" -eq 1 ] && echo "     the api applies the migrations on start ($backup_rev -> $code_head); or run: make migrate" )
  2. verify the bundles:     docker compose exec api python -m orchestrator.bundles --store db --root /app/data/$bundles_subdir verify
  3. check the API:          make smoke
EOF
