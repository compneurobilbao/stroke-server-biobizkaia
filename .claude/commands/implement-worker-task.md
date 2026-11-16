---
description: Add or change a Celery task or orchestrator/executor code with clear states, cancellation and tests.
---

# Implement a worker or orchestrator task

Task or card: $ARGUMENTS

- Celery tasks live in `worker/tasks.py`; executors in `orchestrator/executors/` behind the `Executor` interface in `base.py` (`submit`, `poll`, `cancel`, `collect`, `cleanup`). Pipeline orchestration follows ADR-0002 and ADR-0003.
- Tasks are idempotent where possible, persist clear states, log in English and audit relevant transitions.
- Cancellation uses the `cancel_requested_at` flag, never message text; executors stop the whole process group.
- External tools run only as contract steps through an executor. The legacy dummy and `compneuro-anatproc` paths go through `processor_adapter/executor_bridge.py`; do not add new adapters.
- Changes to `orchestrator/` need Opus review (plan section 7).
- Add tests in `tests/` (`test_worker.py`, `test_executors.py`) including failure, timeout and cancellation paths.
- Check: `make lint`, `make test` (POSIX environment; CI is the source of truth).
