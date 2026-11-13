"""Platform-side orchestration: executors now, the pipeline orchestrator later (F0-15).

This package is imported by the worker and the backend. It must stay light: no
database, Celery or web imports at module level.
"""
