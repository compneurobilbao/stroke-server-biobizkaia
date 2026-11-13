.PHONY: e2e up down logs test test-steps lint format migrate seed clean smoke check-docs check-secrets frontend-rebuild rebuild create-admin backup restore retention-dry-run retention-execute

WORKER_REPLICAS ?=
DAYS ?= 90

up:
	worker_replicas="$(WORKER_REPLICAS)"; \
	if [ -z "$$worker_replicas" ] && [ -f .env ]; then \
		worker_replicas="$$(awk -F= '/^[[:space:]]*WORKER_REPLICAS[[:space:]]*=/{value=$$0; sub(/^[^=]*=/, "", value); gsub(/^[[:space:]]+|[[:space:]]+$$/, "", value); print value; exit}' .env)"; \
	fi; \
	docker compose up -d --scale worker=$${worker_replicas:-1}

frontend-rebuild:
	docker compose up -d --build --force-recreate frontend

rebuild:
	worker_replicas="$(WORKER_REPLICAS)"; \
	if [ -z "$$worker_replicas" ] && [ -f .env ]; then \
		worker_replicas="$$(awk -F= '/^[[:space:]]*WORKER_REPLICAS[[:space:]]*=/{value=$$0; sub(/^[^=]*=/, "", value); gsub(/^[[:space:]]+|[[:space:]]+$$/, "", value); print value; exit}' .env)"; \
	fi; \
	docker compose up -d --build --force-recreate --scale worker=$${worker_replicas:-1}

down:
	docker compose down

logs:
	docker compose logs -f

test:
	PYTHONPATH=.:backend pytest tests

# Step SDK and template step unit tests. They need the SDK's own pinned dependencies
# (steps/sdk/requirements-image.txt, differing from backend/requirements.txt) and the SDK
# installed, so run them in a separate virtualenv:
#   pip install -r steps/sdk/requirements-image.txt pytest==8.3.3 && pip install --no-deps -e steps/sdk
test-steps:
	python -m pytest steps -q

lint:
	ruff check backend worker processor_adapter tests

format:
	ruff format backend worker processor_adapter tests

migrate:
	docker compose exec api alembic upgrade head

create-admin:
	test -n "$(EMAIL)" || (echo "Usage: make create-admin EMAIL=admin@example.org"; exit 1)
	docker compose exec api python -m app.cli.create_admin --email "$(EMAIL)"

retention-dry-run:
	docker compose exec api python -m app.cli.retention --days "$(DAYS)"

retention-execute:
	docker compose exec api python -m app.cli.retention --days "$(DAYS)" --execute

backup:
	sh ./scripts/backup.sh

restore:
	test -n "$(BACKUP_DIR)" || (echo "Usage: make restore BACKUP_DIR=backups/<timestamp> CONFIRM_RESTORE=YES_I_UNDERSTAND"; exit 1)
	BACKUP_DIR="$(BACKUP_DIR)" CONFIRM_RESTORE="$(CONFIRM_RESTORE)" sh ./scripts/restore.sh

seed:
	./scripts/seed.sh

clean:
	docker compose down -v
	rm -rf data/studies/*
	touch data/studies/.gitkeep

smoke:
	./scripts/smoke.sh

check-docs:
	./scripts/check-docs.sh

check-secrets:
	./scripts/check-no-secrets.sh

# End-to-end test against a running instance with a public OpenNeuro T1w (CC0).
# Needs E2E_EMAIL and E2E_PASSWORD of a dedicated test account; E2E_BASE_URL defaults to http://localhost.
e2e:
	python3 scripts/e2e_openneuro_t1w.py
