# Worker

This folder contains the asynchronous Celery process. Its job is to run long processing outside the HTTP request-response cycle.

## Responsibility

- Consume tasks from Redis.
- Change the states of `Study` and `ProcessingJob`.
- Invoke `processor_adapter`.
- Render NIfTI results to PNG with FSL `slicer` when the backend is `compneuro`.
- Store the paths of the PDF, ZIP, PNGs and technical logs.
- Mark failures in a controlled way.
- Respond to cancellation of running tasks and propagate the signal to the external processor.
- Record internal notifications and send optional emails when a processing run closes.

## Code Layout

- `celery_app.py`: creates the Celery application using `CELERY_BROKER_URL` and `CELERY_RESULT_BACKEND`.
- `tasks.py`: defines the `process_study` task.
- `__init__.py`: marks the directory as a Python package.

## `process_study` Task Flow

1. Receives `study_id` and `job_id`.
2. Fetches the records from PostgreSQL.
3. Marks the study as `processing`.
4. Builds the `input`, `output` and `logs` paths with `LocalStudyStorage`.
5. Runs `ProcessorAdapter.run(...)`.
6. For `compneuro`, looks for NIfTI files in `output/Preproc` and generates PNGs, the technical PDF and the ZIP if enabled.
7. If there is a PDF, marks `completed` and stores `pdf_path`.
8. On failure, marks `failed` and stores a short technical message.
9. On cancellation, marks `canceled` and keeps the traceability of the attempt.
10. Records audit events and closing notifications.

## Critical Limit

The worker must not implement image analysis or medical interpretation. It only coordinates execution, states, logs and persistence. The external processor is entered through `processor_adapter`.
