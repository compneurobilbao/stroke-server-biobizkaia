from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "neuroimagen_worker",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["worker.tasks"],
)

# Pipeline drivers run for hours; they go to their own queue so that they never block
# legacy ``process_study`` tasks. Only task_routes is set (no task_queues): a worker
# started without ``-Q`` therefore keeps consuming just the default queue, and only
# the pipeline-worker service (``-Q pipelines``) consumes this one.
PIPELINE_QUEUE = "pipelines"

celery_app.conf.update(
    task_routes={
        "worker.tasks.run_pipeline": {"queue": PIPELINE_QUEUE},
        "worker.tasks.recover_pipeline_runs": {"queue": PIPELINE_QUEUE},
    },
    task_track_started=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    worker_prefetch_multiplier=1,
    # Crash recovery of pipeline runs (docs/data-model.md 7.1). Only effective
    # when a beat process runs (celery -A worker.celery_app.celery_app beat).
    beat_schedule={
        "recover-pipeline-runs": {
            "task": "worker.tasks.recover_pipeline_runs",
            "schedule": 300.0,
        }
    },
)
