#!/usr/bin/env bash
# Consistent backup of a Docker Compose deployment (card OPS-01, docs/operations/backup-restore.md).
#
#   scripts/ops/backup.sh [options]
#
# What it writes, into <dest>/stroke-server-backup-<UTC timestamp>/:
#   db.dump           pg_dump -Fc of the application database, taken inside the postgres container
#                     with the credentials of that container's environment (never read or printed here)
#   data.tar.gz       the data tree (studies, cases, run work and outputs, bundle registry file),
#                     without the reference bundle payloads and without pre-restore copies
#   bundles.tar.gz    only with --include-bundles: the whole bundle root
#   bundles.tsv       inventory of the registered bundles (id, version, storage path, digests)
#   manifest.json     sizes, SHA-256, alembic revision, pipeline/registry/platform versions, git commit
#   SHA256SUMS        the same checksums in `sha256sum -c` format
#
# The application may keep running: the dump is a single-transaction snapshot and the data tree is
# archived after it, so every file that the dump references already exists when it is archived.
# Files of runs still in progress may be partial (see the documentation).
#
# Options:
#   -p, --project-name NAME   Compose project name (default: Compose's own default)
#   -C, --compose-dir DIR     directory with docker-compose.yml (default: repository root)
#   -d, --data-dir DIR        host directory mounted as /app/data (default: <compose-dir>/data)
#   -o, --dest DIR            where backups are written (default: <compose-dir>/backups);
#                             refused when it is inside the data directory
#   -k, --keep N              keep only the newest N complete backups in --dest (default: 0 = all)
#       --include-bundles     also archive the reference bundle payloads (large; re-registrable)
#       --bundles-subdir DIR  bundle root relative to the data directory (default: bundles)
#   -h, --help
set -euo pipefail
umask 077

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
COMPOSE_DIR="$(cd "$SCRIPT_DIR/../.." && pwd -P)"
PROJECT_NAME=""
DATA_DIR=""
DEST=""
KEEP=0
INCLUDE_BUNDLES=0
BUNDLES_SUBDIR="bundles"
FORMAT_VERSION=1

usage() { sed -n '2,/^set -euo/p' "${BASH_SOURCE[0]}" | sed '$d; s/^# \{0,1\}//'; }
die() { printf 'backup: %s\n' "$*" >&2; exit 1; }
log() { printf 'backup: %s\n' "$*"; }

while [ $# -gt 0 ]; do
  case "$1" in
    -p|--project-name) PROJECT_NAME="${2:?}"; shift 2 ;;
    -C|--compose-dir) COMPOSE_DIR="${2:?}"; shift 2 ;;
    -d|--data-dir) DATA_DIR="${2:?}"; shift 2 ;;
    -o|--dest) DEST="${2:?}"; shift 2 ;;
    -k|--keep) KEEP="${2:?}"; shift 2 ;;
    --include-bundles) INCLUDE_BUNDLES=1; shift ;;
    --bundles-subdir) BUNDLES_SUBDIR="${2:?}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

case "$KEEP" in ''|*[!0-9]*) die "--keep needs a non-negative integer" ;; esac
case "$BUNDLES_SUBDIR" in ''|/*|*..*|*/*) die "--bundles-subdir must be a single relative directory name" ;; esac

[ -f "$COMPOSE_DIR/docker-compose.yml" ] || die "no docker-compose.yml in $COMPOSE_DIR"
COMPOSE_DIR="$(cd "$COMPOSE_DIR" && pwd -P)"
DATA_DIR="${DATA_DIR:-$COMPOSE_DIR/data}"
DEST="${DEST:-$COMPOSE_DIR/backups}"
[ -d "$DATA_DIR" ] || die "data directory does not exist: $DATA_DIR"
DATA_DIR="$(cd "$DATA_DIR" && pwd -P)"
# Resolve the destination before creating anything, so a refused destination leaves no trace.
DEST="$(realpath -m -- "$DEST")"
case "$DEST/" in
  "$DATA_DIR"/*) die "refusing to write the backup inside the data directory ($DATA_DIR)" ;;
esac
mkdir -p "$DEST"
DEST="$(cd "$DEST" && pwd -P)"
case "$DEST/" in
  "$DATA_DIR"/*) die "refusing to write the backup inside the data directory ($DATA_DIR)" ;;
esac

compose() {
  if [ -n "$PROJECT_NAME" ]; then
    docker compose --project-directory "$COMPOSE_DIR" -p "$PROJECT_NAME" "$@"
  else
    docker compose --project-directory "$COMPOSE_DIR" "$@"
  fi
}

# psql inside the postgres container. The credentials are expanded by the container's shell from
# its own environment; the single quotes keep them out of this script, its arguments and its logs.
psql_at() {
  compose exec -T postgres sh -c 'psql -X -At -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' <<<"$1"
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | cut -d' ' -f1
  else shasum -a 256 "$1" | cut -d' ' -f1; fi
}
size_of() { wc -c <"$1" | tr -d ' '; }
json_str() {
  local s="$1"
  s="${s//\\/\\\\}"; s="${s//\"/\\\"}"; s="${s//$'\t'/\\t}"; s="${s//$'\n'/\\n}"; s="${s//$'\r'/}"
  printf '"%s"' "$s"
}

# Runs a shell snippet with SRC (the data directory) and OUT (the backup directory being written).
# As root it runs on the host; otherwise in a throwaway container of the api image (root inside),
# because files written by the containers are often not readable by the invoking user. tar keeps
# numeric owners and modes either way, so a restore can put them back.
in_data_env() {
  local mode="$1" script="$2"
  if [ "$(id -u)" -eq 0 ]; then
    SRC="$DATA_DIR" OUT="$WORK" bash -c "$script"
  else
    compose run --rm --no-deps -T --entrypoint bash \
      -v "$DATA_DIR:/backup-src:$mode" -v "$WORK:/backup-out" \
      -e SRC=/backup-src -e OUT=/backup-out \
      api -c "trap 'chown -R $(id -u):$(id -g) /backup-out' EXIT
$script"
  fi
}

# ---------------------------------------------------------------- preconditions
command -v docker >/dev/null 2>&1 || die "docker is not installed"
compose ps --services --status running 2>/dev/null | grep -qx postgres \
  || die "the postgres service of this Compose project is not running"

lockfile="$DEST/.backup.lock"
if command -v flock >/dev/null 2>&1; then
  exec 9>"$lockfile"
  flock -n 9 || die "another backup is writing to $DEST"
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
NAME="stroke-server-backup-$stamp"
FINAL="$DEST/$NAME"
WORK="$DEST/.tmp-$NAME-$$"
[ -e "$FINAL" ] && die "$FINAL already exists"

done_ok=0
cleanup() {
  if [ "$done_ok" -ne 1 ] && [ -d "$WORK" ]; then
    rm -rf "$WORK" 2>/dev/null || log "could not remove the partial backup $WORK; remove it by hand"
  fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM
mkdir -p "$WORK"

# ---------------------------------------------------------------- database
alembic_revision="$(psql_at 'SELECT version_num FROM alembic_version' 2>/dev/null | head -n1 || true)"
pg_server="$(psql_at 'SHOW server_version' | head -n1)"
log "dumping the database (PostgreSQL $pg_server, alembic revision ${alembic_revision:-none})"
compose exec -T postgres sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >"$WORK/db.dump"
[ -s "$WORK/db.dump" ] || die "pg_dump produced an empty file"

# Inventory of the registered bundles (the payloads are not in the default backup).
{
  printf 'bundle_id\tversion\tstorage_path\tcontent_digest\tmanifest_sha256\tstatus\tredistributable\n'
  psql_at "SELECT bundle_id || E'\t' || version || E'\t' || storage_path || E'\t' || content_digest
           || E'\t' || manifest_sha256 || E'\t' || status || E'\t' || redistributable
           FROM reference_bundles ORDER BY bundle_id, version" 2>/dev/null || true
} >"$WORK/bundles.tsv"

runs_by_pipeline="$(psql_at "SELECT pipeline_id || ' ' || pipeline_version || ' ' || count(*)
                             FROM pipeline_runs GROUP BY 1 ORDER BY 1" 2>/dev/null || true)"
counts_json="$(psql_at "SELECT '{\"cases\": ' || (SELECT count(*) FROM cases) || ', \"pipeline_runs\": '
                         || (SELECT count(*) FROM pipeline_runs) || '}'" 2>/dev/null || true)"
[ -n "$counts_json" ] || counts_json="{}"

# ---------------------------------------------------------------- files
log "archiving the data tree from $DATA_DIR (bundle payloads $([ "$INCLUDE_BUNDLES" -eq 1 ] && echo included || echo excluded))"
# GNU tar exits 1 when a file changed while it was read (a live deployment); that is reported, not
# fatal. Everything else is fatal.
tar_script='
set -euo pipefail
cd "$SRC"
B="'"$BUNDLES_SUBDIR"'"
list="$OUT/.data-files"
: >"$list"
find . -mindepth 1 -maxdepth 1 ! -name "$B" ! -name pre-restore -print0 >>"$list"
[ -f "$B/.registry.json" ] && printf "%s\0" "./$B/.registry.json" >>"$list"
rc=0
tar --create --gzip --numeric-owner --null --files-from="$list" -f "$OUT/data.tar.gz" || rc=$?
rm -f "$list"
if [ "$rc" -eq 1 ]; then echo "backup: warning: some files changed while they were archived (live deployment)" >&2
elif [ "$rc" -ne 0 ]; then exit "$rc"; fi
if [ '"$INCLUDE_BUNDLES"' -eq 1 ] && [ -d "$B" ]; then
  rc=0
  tar --create --gzip --numeric-owner --exclude="./$B/.incoming-*" --exclude="./$B/*/.incoming-*" \
    -f "$OUT/bundles.tar.gz" "./$B" || rc=$?
  if [ "$rc" -eq 1 ]; then echo "backup: warning: bundle files changed while they were archived" >&2
  elif [ "$rc" -ne 0 ]; then exit "$rc"; fi
fi
'
in_data_env ro "$tar_script"
[ -s "$WORK/data.tar.gz" ] || die "the data archive was not written"

# ---------------------------------------------------------------- manifest
git_commit="$(git -C "$COMPOSE_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
git_dirty=false
if [ "$git_commit" != unknown ] && [ -n "$(git -C "$COMPOSE_DIR" status --porcelain --untracked-files=no 2>/dev/null)" ]; then
  git_dirty=true
fi
platform_version="$(compose exec -T api sh -c 'printf %s "${PLATFORM_VERSION:-}"' 2>/dev/null || true)"

files=(db.dump data.tar.gz bundles.tsv)
[ -f "$WORK/bundles.tar.gz" ] && files+=(bundles.tar.gz)
(cd "$WORK" && for f in "${files[@]}"; do printf '%s  %s\n' "$(sha256_of "$f")" "$f"; done) >"$WORK/SHA256SUMS"

{
  printf '{\n'
  printf '  "format_version": %s,\n' "$FORMAT_VERSION"
  printf '  "name": %s,\n' "$(json_str "$NAME")"
  printf '  "created_at": %s,\n' "$(json_str "$(date -u +%Y-%m-%dT%H:%M:%SZ)")"
  printf '  "compose_project": %s,\n' "$(json_str "${PROJECT_NAME:-default}")"
  printf '  "git_commit": %s,\n' "$(json_str "$git_commit")"
  printf '  "git_dirty": %s,\n' "$git_dirty"
  printf '  "alembic_revision": %s,\n' "$(json_str "$alembic_revision")"
  printf '  "postgres_server_version": %s,\n' "$(json_str "$pg_server")"
  printf '  "platform_version": %s,\n' "$(json_str "${platform_version:-unknown}")"
  printf '  "bundles_included": %s,\n' "$([ "$INCLUDE_BUNDLES" -eq 1 ] && echo true || echo false)"
  printf '  "bundles_subdir": %s,\n' "$(json_str "$BUNDLES_SUBDIR")"
  printf '  "counts": %s,\n' "$counts_json"
  printf '  "pipelines": ['
  sep=""
  for p in "$COMPOSE_DIR"/pipelines/*/pipeline.yaml; do
    [ -f "$p" ] || continue
    d="$(dirname "$p")"
    pid="$(sed -n 's/^id:[[:space:]]*//p' "$p" | head -n1)"
    pver="$(sed -n 's/^version:[[:space:]]*//p' "$p" | head -n1)"
    rver="$(sed -n 's/^registry_version:[[:space:]]*//p' "$d/registry.yaml" 2>/dev/null | head -n1)"
    printf '%s\n    {"id": %s, "version": %s, "registry_version": %s}' "$sep" \
      "$(json_str "$pid")" "$(json_str "$pver")" "$(json_str "${rver:-}")"
    sep=","
  done
  printf '\n  ],\n'
  printf '  "runs_by_pipeline": ['
  sep=""
  while read -r rp rv rc; do
    [ -n "$rp" ] || continue
    printf '%s\n    {"pipeline_id": %s, "pipeline_version": %s, "runs": %s}' "$sep" "$(json_str "$rp")" "$(json_str "$rv")" "$rc"
    sep=","
  done <<<"$runs_by_pipeline"
  printf '\n  ],\n'
  printf '  "files": ['
  sep=""
  for f in "${files[@]}"; do
    printf '%s\n    {"name": %s, "size": %s, "sha256": %s}' "$sep" "$(json_str "$f")" \
      "$(size_of "$WORK/$f")" "$(json_str "$(sha256_of "$WORK/$f")")"
    sep=","
  done
  printf '\n  ]\n}\n'
} >"$WORK/manifest.json"

mv "$WORK" "$FINAL"
done_ok=1
log "backup written to $FINAL ($(du -sh "$FINAL" | cut -f1))"

# ---------------------------------------------------------------- retention
if [ "$KEEP" -gt 0 ]; then
  mapfile -t complete < <(find "$DEST" -mindepth 1 -maxdepth 1 -type d -name 'stroke-server-backup-*' \
    -exec test -f '{}/manifest.json' ';' -print | sort)
  excess=$(( ${#complete[@]} - KEEP ))
  for ((i = 0; i < excess; i++)); do
    log "retention: removing ${complete[$i]}"
    rm -rf -- "${complete[$i]}"
  done
fi
