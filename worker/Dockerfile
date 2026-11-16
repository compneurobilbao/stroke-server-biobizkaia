# Slim orchestrator worker (card F0-15): platform code only, no processor inside.
#
# Pipeline steps run as their own containers through the docker executor (socket
# proxy, docs/deployment.md) or on the HPC; this image only orchestrates. It has
# neither the dummy development processor nor compneuro-anatproc, so legacy
# studies cannot be processed by it: deployments that still accept legacy uploads
# keep WORKER_DOCKERFILE=worker/Dockerfile.compneuro (or backend/Dockerfile for
# the dummy processor). Select this image with WORKER_DOCKERFILE=worker/Dockerfile.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r /app/backend/requirements.txt

COPY backend /app/backend
COPY worker /app/worker
COPY orchestrator /app/orchestrator
COPY contracts /app/contracts
# The legacy task module imports the adapter package; its processors are not installed.
COPY processor_adapter /app/processor_adapter

ENV PYTHONPATH=/app:/app/backend
WORKDIR /app/backend

CMD ["celery", "-A", "worker.celery_app.celery_app", "worker", "--loglevel=INFO", "--concurrency=1"]
