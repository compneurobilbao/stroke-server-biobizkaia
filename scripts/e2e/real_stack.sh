#!/usr/bin/env bash
# Test-only helper of the real-stack Playwright job (card F0-31a). It prepares and starts the
# Compose stack with the `pipelines` profile on the pieces that the e2e-real-stack workflow built
# (materialised pipeline, step manifests read from the images, reference bundles). Not for
# deployments: it writes a throwaway .env and generates throwaway credentials.
#
#   real_stack.sh layout     copy the build outputs under ./data/e2e (mounted into api and workers)
#   real_stack.sh env        write ./.env for the stack (random secrets, executor host path map)
#   real_stack.sh up         docker compose --profile pipelines up -d --build, wait for the API
#   real_stack.sh bundles    register the reference bundles in the database store
#   real_stack.sh seed       seed slot_implementation_settings from the `ci` profile
#   real_stack.sh users      create a throwaway admin, then a researcher through the users API
#   real_stack.sh logs       print the logs of every service (for failures)
#
# Inputs (environment):
#   WORK                  directory with pipelines/, step-manifests/ and bundles/ (layout)
#   E2E_ADMIN_EMAIL, E2E_ADMIN_PASSWORD, E2E_USER_EMAIL, E2E_USER_PASSWORD   (users)
#   E2E_BASE_URL          defaults to http://localhost
set -euo pipefail

cd "$(dirname "$0")/../.."
ROOT="$PWD"
BASE_URL="${E2E_BASE_URL:-http://localhost}"
SERVICE_DIR="data/e2e"

compose() { docker compose --profile pipelines "$@"; }
# -T: no pseudo-terminal in CI
exec_api() { compose exec -T api "$@"; }

cmd_layout() {
  : "${WORK:?WORK is not set}"
  rm -rf "$SERVICE_DIR"
  mkdir -p "$SERVICE_DIR/incoming"
  cp -r "$WORK/pipelines" "$SERVICE_DIR/pipelines"
  cp -r "$WORK/step-manifests" "$SERVICE_DIR/step-manifests"
  cp -r "$WORK/bundles/." "$SERVICE_DIR/incoming/"
  # Step containers run as an unrelated numeric user and must traverse and read these.
  chmod -R a+rX "$SERVICE_DIR"
  find "$SERVICE_DIR" -maxdepth 3 -print
}

cmd_env() {
  local secret
  secret="$(openssl rand -hex 32)"
  {
    echo "ENVIRONMENT=development"
    echo "AUTH_SECRET_KEY=$secret"
    echo "NOTIFICATIONS_EMAIL_ENABLED=false"
    echo "APP_PUBLIC_BASE_URL=$BASE_URL"
    echo "DEPLOYMENT_PROFILE=research-public"
    echo "COMPOSE_PROFILES=pipelines"
    echo "PIPELINES_DIR=/app/$SERVICE_DIR/pipelines"
    echo "STEP_MANIFESTS_DIR=/app/$SERVICE_DIR/step-manifests"
    echo "API_PIPELINES_DIR=/app/$SERVICE_DIR/pipelines"
    echo "API_STEP_MANIFESTS_DIR=/app/$SERVICE_DIR/step-manifests"
    echo "BUNDLE_ROOT=/app/data/bundles"
    echo "ORCHESTRATOR_EXECUTOR=docker"
    echo "ORCHESTRATOR_POLL_SECONDS=2"
    echo "ORCHESTRATOR_MAX_ATTEMPTS=1"
    # Worker path -> path of the same directory as the Docker host sees it (docs/deployment.md).
    echo "DOCKER_EXECUTOR_HOST_PATH_MAP=/app/data=$ROOT/data"
  } > .env
  # Show the shape, never the secret.
  sed 's/^AUTH_SECRET_KEY=.*/AUTH_SECRET_KEY=<generated>/' .env
}

cmd_up() {
  compose config --services
  compose up -d --build
  for _ in $(seq 1 90); do
    if curl -fsS "$BASE_URL/api/health" > /dev/null 2>&1; then
      echo "API is up"
      compose ps
      return 0
    fi
    sleep 2
  done
  echo "API did not become healthy" >&2
  compose ps >&2
  return 1
}

cmd_bundles() {
  local dir
  for dir in "$SERVICE_DIR"/incoming/*/; do
    dir="${dir%/}"
    exec_api python -m orchestrator.bundles --store db --root /app/data/bundles register "/app/$dir"
  done
  exec_api python -m orchestrator.bundles --store db --root /app/data/bundles list
}

cmd_seed() {
  # The command behind `make seed-implementations PROFILE=ci` (docs/deployment.md), with -T.
  exec_api python -m app.cli.seed_implementations --profile ci
}

json_field() { python3 -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"; }

cmd_users() {
  : "${E2E_ADMIN_EMAIL:?}" "${E2E_ADMIN_PASSWORD:?}" "${E2E_USER_EMAIL:?}" "${E2E_USER_PASSWORD:?}"
  exec_api python -m app.cli.create_admin --email "$E2E_ADMIN_EMAIL" \
    --full-name "E2E Administrator" --password "$E2E_ADMIN_PASSWORD"
  local token body
  body="$(python3 -c 'import json,os; print(json.dumps({"email": os.environ["E2E_ADMIN_EMAIL"], "password": os.environ["E2E_ADMIN_PASSWORD"]}))')"
  token="$(curl -fsS -H 'Content-Type: application/json' -d "$body" "$BASE_URL/api/auth/login" | json_field access_token)"
  echo "::add-mask::$token"
  body="$(python3 -c 'import json,os; print(json.dumps({"email": os.environ["E2E_USER_EMAIL"], "full_name": "E2E Researcher", "password": os.environ["E2E_USER_PASSWORD"], "role": "researcher"}))')"
  curl -fsS -H 'Content-Type: application/json' -H "Authorization: Bearer $token" -d "$body" \
    "$BASE_URL/api/users" | json_field role
}

cmd_logs() {
  compose ps -a || true
  compose logs --no-color --timestamps || true
}

case "${1:-}" in
  layout) cmd_layout ;;
  env) cmd_env ;;
  up) cmd_up ;;
  bundles) cmd_bundles ;;
  seed) cmd_seed ;;
  users) cmd_users ;;
  logs) cmd_logs ;;
  *) sed -n '2,17p' "$0"; exit 2 ;;
esac
