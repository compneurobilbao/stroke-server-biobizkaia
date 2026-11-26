#!/usr/bin/env bash
# CI test of scripts/ops/backup.sh and scripts/ops/restore.sh on a throwaway Docker Compose stack
# (card OPS-01, .github/workflows/backup-restore.yml). DESTRUCTIVE: it removes the stack's volumes
# and the data directory of this checkout, so it only runs with OPS_TEST_DESTROY_STACK=yes and
# never over an existing .env.
#
# Covered: backup while the api runs (container credentials never leave the container), refusal to
# write inside the data directory, retention, the default backup without bundle payloads restored
# next to bundles that stayed on the server, a disk-loss restore with --include-bundles, refusals
# of a restore while the api runs and over a database with data, --force, checksum verification,
# and that a seeded case, its succeeded run and a run output file (content, owner, mode) survive.
set -euo pipefail

[ "${OPS_TEST_DESTROY_STACK:-}" = yes ] || { echo "set OPS_TEST_DESTROY_STACK=yes (destroys the stack and ./data)" >&2; exit 2; }
cd "$(dirname "$0")/../.."
ROOT="$PWD"
[ ! -e .env ] || { echo ".env exists; refusing to overwrite it" >&2; exit 2; }

PROJECT=ops-backup-test
BK="${OPS_TEST_BACKUP_DIR:-$ROOT/../ops-backup-test-backups}"
mkdir -p "$BK"
BK="$(cd "$BK" && pwd -P)"
LOG="$BK/test.log"
: >"$LOG"
dc() { docker compose -p "$PROJECT" "$@"; }
step() { printf '\n==== %s\n' "$*"; }
fail() { printf 'FAIL: %s\n' "$*" >&2; exit 1; }
expect_fail() {
  local what="$1"; shift
  if "$@" >>"$LOG" 2>&1; then fail "$what: the command succeeded but should have been refused"; fi
  echo "refused as expected: $what"
}
wait_api() {
  for _ in $(seq 1 90); do
    if dc exec -T api python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health', timeout=5)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  dc logs api | tail -n 50
  fail "the api did not become healthy"
}
wipe_data() {  # $1 = keep-bundles | all; files belong to container users, so remove them from a container
  local keep="$1"
  dc run --rm --no-deps -T --entrypoint bash -e KEEP="$keep" api -c '
    cd /app/data
    if [ "$KEEP" = keep-bundles ]; then find . -mindepth 1 -maxdepth 1 ! -name bundles -exec rm -rf {} +
    else find . -mindepth 1 -maxdepth 1 -exec rm -rf {} +; fi'
  git checkout -- data/README.md data/studies/.gitkeep 2>/dev/null || true
}
backup() { bash scripts/ops/backup.sh -p "$PROJECT" --dest "$BK" "$@" 2>&1 | tee -a "$LOG"; }
restore() { bash scripts/ops/restore.sh -p "$PROJECT" "$@" 2>&1 | tee -a "$LOG"; }

# Throwaway credentials that differ from the defaults, to check they never reach the backup.
DB_PASSWORD="$(openssl rand -hex 16)"
echo "::add-mask::$DB_PASSWORD"
{
  echo "POSTGRES_PASSWORD=$DB_PASSWORD"
  echo "DATABASE_URL=postgresql+psycopg://neuroimagen:$DB_PASSWORD@postgres:5432/neuroimagen"
  echo "AUTH_SECRET_KEY=$(openssl rand -hex 32)"
  echo "NOTIFICATIONS_EMAIL_ENABLED=false"
  echo "PLATFORM_VERSION=0.0.0-ops-test"
} >.env
trap 'rm -f "$ROOT/.env"' EXIT

step "start postgres, redis and the api"
dc up -d --build postgres redis api
wait_api

step "seed a case, a succeeded run, a run output file and a registered bundle"
mkdir -p data/incoming
cp -r fixtures/data/bundles/toy-tract-atlas/1.0.0 data/incoming/toy-tract-atlas
dc exec -T api python -m orchestrator.bundles --store db --root /app/data/bundles register /app/data/incoming/toy-tract-atlas
rm -rf data/incoming
SEED="$(dc exec -T api python - <<'EOF'
import datetime, hashlib, json, os, pathlib
from app.db.session import SessionLocal
from app.models import Case, PipelineRun, User

with SessionLocal() as db:
    user = User(email="ops-test@example.org", full_name="Ops Test", role="researcher")
    db.add(user); db.commit()
    case = Case(owner_user_id=user.id, deployment_profile="research-public")
    db.add(case); db.commit()
    now = datetime.datetime.now(datetime.timezone.utc)
    run = PipelineRun(
        case_id=case.id, created_by_user_id=user.id, pipeline_id="stroke-lnm",
        pipeline_version="0.11.0", pipeline_sha256="a" * 64, platform_version="0.0.0-ops-test",
        artefact_types_version="1.0", input_set="mni-mask", seed=42, status="succeeded",
        started_at=now, finished_at=now, accounting_submitted_by=f"user:{user.id}",
        accounting_submit_as="service-account",
    )
    db.add(run); db.commit()
    out = pathlib.Path(os.environ["STORAGE_ROOT"]) / str(case.id) / "work" / str(run.id) / "report" / "result.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(os.urandom(4096))
    os.chown(out, 4321, 4321); os.chmod(out, 0o640)
    print(json.dumps({"case": str(case.id), "run": str(run.id), "file": str(out),
                      "sha256": hashlib.sha256(out.read_bytes()).hexdigest()}))
EOF
)"
echo "$SEED"
field() { python3 -c 'import json,sys; print(json.loads(sys.argv[1])[sys.argv[2]])' "$SEED" "$1"; }
CASE_ID="$(field case)"; RUN_ID="$(field run)"; OUT_FILE="$(field file)"; OUT_SHA="$(field sha256)"

verify() {
  dc exec -T -e CASE_ID="$CASE_ID" -e RUN_ID="$RUN_ID" -e OUT_FILE="$OUT_FILE" -e OUT_SHA="$OUT_SHA" api python - <<'EOF'
import hashlib, os, pathlib, uuid
from app.db.session import SessionLocal
from app.models import Case, PipelineRun

with SessionLocal() as db:
    case = db.get(Case, uuid.UUID(os.environ["CASE_ID"]))
    run = db.get(PipelineRun, uuid.UUID(os.environ["RUN_ID"]))
    assert case is not None, "case missing"
    assert run is not None and run.case_id == case.id and run.status == "succeeded", "run missing or changed"
out = pathlib.Path(os.environ["OUT_FILE"])
st = out.stat()
assert hashlib.sha256(out.read_bytes()).hexdigest() == os.environ["OUT_SHA"], "run output content differs"
assert (st.st_uid, st.st_gid) == (4321, 4321), f"owner not preserved: {st.st_uid}:{st.st_gid}"
assert st.st_mode & 0o777 == 0o640, f"mode not preserved: {oct(st.st_mode)}"
print("case, run and run output restored")
EOF
  dc exec -T api python -m orchestrator.bundles --store db --root /app/data/bundles verify
}

step "refusals of backup.sh"
expect_fail "backup inside the data directory" bash scripts/ops/backup.sh -p "$PROJECT" --dest "$ROOT/data/backups"

step "backups while the api runs, with retention"
backup --keep 2
sleep 1
backup --include-bundles --keep 2
sleep 1
backup --keep 2
mapfile -t backups < <(find "$BK" -mindepth 1 -maxdepth 1 -type d -name 'stroke-server-backup-*' | sort)
[ "${#backups[@]}" -eq 2 ] || fail "retention kept ${#backups[@]} backups instead of 2"
WITH_BUNDLES="${backups[0]}"
DEFAULT="${backups[1]}"
[ -f "$WITH_BUNDLES/bundles.tar.gz" ] || fail "--include-bundles did not archive the bundles"
[ ! -f "$DEFAULT/bundles.tar.gz" ] || fail "the default backup archived the bundles"
if tar -tzf "$DEFAULT/data.tar.gz" | grep -q '^\./bundles/toy-tract-atlas/'; then fail "bundle payload in data.tar.gz"; fi
tar -tzf "$DEFAULT/data.tar.gz" | grep -q "$RUN_ID/report/result.txt" || fail "run output missing from data.tar.gz"
grep -q 'toy-tract-atlas' "$DEFAULT/bundles.tsv" || fail "bundle inventory incomplete"
grep -q '"alembic_revision": "0' "$DEFAULT/manifest.json" || fail "no alembic revision in the manifest"
grep -q '"platform_version": "0.0.0-ops-test"' "$DEFAULT/manifest.json" || fail "no platform version in the manifest"
grep -q '"pipeline_id": "stroke-lnm", "pipeline_version": "0.11.0", "runs": 1' "$DEFAULT/manifest.json" || fail "runs_by_pipeline incomplete"
grep -q '"counts": {"cases": 1, "pipeline_runs": 1}' "$DEFAULT/manifest.json" || fail "counts missing from the manifest"
cat "$DEFAULT/manifest.json"
if grep -rqF "$DB_PASSWORD" "$BK"; then fail "the database password reached the backup directory or the logs"; fi

step "restore.sh refuses while the api runs and a damaged backup"
expect_fail "restore while the api runs" bash scripts/ops/restore.sh -p "$PROJECT" "$DEFAULT"
dc stop api
DAMAGED="$BK/damaged"
cp -r "$DEFAULT" "$DAMAGED"
printf 'x' >>"$DAMAGED/data.tar.gz"
expect_fail "restore of a damaged backup" bash scripts/ops/restore.sh -p "$PROJECT" "$DAMAGED"
rm -rf "$DAMAGED"

step "scenario A: database and data lost, bundles stayed on the server; default backup"
dc down -v
wipe_data keep-bundles
restore "$DEFAULT"
expect_fail "restore over a database with data" bash scripts/ops/restore.sh -p "$PROJECT" "$DEFAULT"
dc up -d api
wait_api
verify

step "scenario B: disk lost, backup with bundles"
dc down -v
wipe_data all
restore "$WITH_BUNDLES"
dc up -d api
wait_api
verify

step "scenario C: --force over a populated deployment"
dc stop api
restore --force "$DEFAULT"
[ -n "$(find data/pre-restore -mindepth 1 -maxdepth 1 2>/dev/null)" ] || fail "--force did not keep the previous data in pre-restore/"
dc up -d api
wait_api
verify

if grep -qF "$DB_PASSWORD" "$LOG"; then fail "the database password reached the script output"; fi
step "backup and restore test passed"
