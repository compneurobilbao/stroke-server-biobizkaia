.PHONY: up down logs test lint format migrate seed clean smoke check-docs check-secrets frontend-rebuild rebuild create-admin backup restore retention-dry-run retention-execute

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

lint:
	ruff check backend worker processor_adapter tests

format:
	ruff format backend worker processor_adapter tests

migrate:
	docker compose exec api alembic upgrade head

create-admin:
	test -n "$(EMAIL)" || (echo "Uso: make create-admin EMAIL=admin@example.org"; exit 1)
	docker compose exec api python -m app.cli.create_admin --email "$(EMAIL)"

retention-dry-run:
	docker compose exec api python -m app.cli.retention --days "$(DAYS)"

retention-execute:
	docker compose exec api python -m app.cli.retention --days "$(DAYS)" --execute

backup:
	sh ./scripts/backup.sh

restore:
	test -n "$(BACKUP_DIR)" || (echo "Uso: make restore BACKUP_DIR=backups/<timestamp> CONFIRM_RESTORE=YES_I_UNDERSTAND"; exit 1)
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
