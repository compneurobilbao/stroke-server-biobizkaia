# Tests

This folder contains basic automated tests. It does not aim to cover the whole system but to validate the highest-risk points: permissions, upload, queue, adapter, worker and technical artefacts.

## Responsibility

- Verify local authentication, roles and per-owner permissions.
- Verify upload, extension validation and basic BIDS.
- Verify job details, truncated logs, cancellation of queued or running jobs, retry and deletion.
- Verify the admin dashboard and its blocking for `researcher`.
- Verify per-user quotas, shared links, notifications and technical review.
- Verify the simulated dummy/compneuro adapter and controlled errors.
- Verify rendering/technical artefacts without running real FSL.
- Verify that the worker marks `failed` if the processor fails and `canceled` if processing is interrupted.

## Code Layout

- `conftest.py`: configures pytest fixtures, a temporary SQLite database and the FastAPI client.
- `test_api.py`: tests auth, permissions, studies, jobs, dashboard, downloads, quotas, sharing, notifications, technical review and actions.
- `test_bids.py`: tests BIDS preparation and safe subject naming.
- `test_processor_adapter.py`: tests the adapter's expected interface with the dummy and error scenarios.
- `test_worker.py`: tests the worker's main transitions on success, error and simulated cancellation.
- `test_notifications.py`: tests SMTP delivery details, including STARTTLS before authentication.

## Running

```bash
make test
```

If run outside a container, first install the dependencies from `backend/requirements.txt`.
