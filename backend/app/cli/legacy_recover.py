"""Find (and with ``--execute`` recover) legacy attempts left without a worker task.

    python -m app.cli.legacy_recover            # dry run: report only
    python -m app.cli.legacy_recover --execute  # re-enqueue / fail as reported

Orphaned queued attempts (older than ``LEGACY_RECOVERY_QUEUED_MINUTES``, task not seen
on any worker) are re-enqueued once; running attempts whose task is not active on any
worker and that started longer ago than ``LEGACY_RECOVERY_RUNNING_HOURS`` (and the
processing timeout) are marked failed with ``legacy_worker_lost``. Running attempts
are only judged when at least one worker answered the inspection. See
worker/legacy_recovery.py and the deployment protocol in docs/deployment.md.
"""

from __future__ import annotations

import argparse
import json

from app.core.config import get_settings
from app.db.session import SessionLocal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Recover legacy attempts left without a worker task"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Re-enqueue and fail as reported. Without it, only report.",
    )
    parser.add_argument(
        "--queued-minutes",
        type=int,
        default=None,
        help="Override LEGACY_RECOVERY_QUEUED_MINUTES for this run",
    )
    args = parser.parse_args(argv)

    from datetime import timedelta

    from worker.celery_app import celery_app
    from worker.legacy_recovery import live_task_ids, recover_legacy_runs, thresholds
    from worker.tasks import _notify_processing_finished, enqueue_attempt

    settings = get_settings()
    queued_after, running_after = thresholds(settings)
    if args.queued_minutes is not None:
        queued_after = timedelta(minutes=max(0, args.queued_minutes))
    report = recover_legacy_runs(
        SessionLocal,
        live_ids=live_task_ids(celery_app),
        enqueue=enqueue_attempt,
        queued_after=queued_after,
        running_after=running_after,
        execute=args.execute,
        on_failed=lambda db, case, run: _notify_processing_finished(
            db, case, run, settings
        ),
    )
    print(json.dumps(report.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
