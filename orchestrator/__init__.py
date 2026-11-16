"""Platform-side orchestration: executors (F0-13/14), bundles (F0-18) and the
pipeline orchestrator (F0-15, ``engine``; overview in docs/processing-pipeline.md).

This package is imported by the worker and the backend. It must stay light: no
database, Celery or web imports at package level. Modules that use the database
(``engine``, ``resolution``, ``actions``, ``events``, ``legacy``, ``provenance``,
``store``) import the backend models themselves and are imported explicitly.
"""
