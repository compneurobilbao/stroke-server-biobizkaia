"""Per-attempt supervisor process of the subprocess executor (POSIX only).

Started by ``SubprocessExecutor.submit`` as ``python -m
orchestrator.executors._supervisor <state_dir>`` in its own session, so that it
survives a restart of the worker process. It:

1. reads ``<state_dir>/spec.json`` (argv, cwd, env, log path, timeout, grace);
2. starts the command in a new process group, stdout and stderr appended in
   order to one log file, stdin closed;
3. records the child's PID and process group in ``child.json``;
4. stops the whole group with SIGTERM, then SIGKILL after the grace period, when
   the timeout expires, when it receives SIGTERM/SIGINT, or when the executor
   creates ``cancel_requested``;
5. after the command exits, kills anything left in its process group;
6. writes ``status.json`` atomically (exit code, signal, stop reason, timestamps,
   usage) and exits 0.

``status.json`` is the single source of truth for ``poll`` and ``collect``: it
does not depend on the worker being the parent of the process. Only the
standard library is used here.
"""

from __future__ import annotations

import json
import os
import resource
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

POLL_SECONDS = 0.1


def _utc_now() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _killpg(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def run(state_dir: Path) -> int:
    spec = json.loads((state_dir / "spec.json").read_text(encoding="utf-8"))
    cancel_marker = state_dir / "cancel_requested"
    stop = {"requested": False}

    def on_signal(_signum, _frame):
        stop["requested"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    timeout = float(spec.get("timeout_seconds") or 0)
    grace = float(spec.get("kill_grace_seconds", 30))
    started_at = _utc_now()
    started = time.monotonic()
    status: dict = {"started_at": started_at}

    with open(spec["log_path"], "ab", buffering=0) as log:
        try:
            child = subprocess.Popen(
                spec["argv"],
                cwd=spec["cwd"],
                env=spec["env"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        except OSError as exc:
            log.write(f"ERROR could not start the command: {exc}\n".encode())
            status.update(
                returncode=None,
                exit_code=127,  # shell convention for "command could not be run"
                signal=None,
                stop_reason=None,
                spawn_error=type(exc).__name__,
                finished_at=_utc_now(),
                usage={"wall_seconds": 0.0},
            )
            _write_json_atomic(state_dir / "status.json", status)
            return 0

        pgid = child.pid
        _write_json_atomic(
            state_dir / "child.json",
            {"pid": child.pid, "pgid": pgid, "started_at": started_at},
        )

        stop_reason = None
        term_sent_at = None
        kill_sent = False
        while True:
            try:
                returncode = child.wait(timeout=POLL_SECONDS)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if stop_reason is None:
                if stop["requested"] or cancel_marker.exists():
                    stop_reason = "canceled"
                elif timeout and now - started >= timeout:
                    stop_reason = "timed_out"
            if stop_reason and term_sent_at is None:
                _killpg(pgid, signal.SIGTERM)
                term_sent_at = now
            elif (
                term_sent_at is not None
                and not kill_sent
                and now - term_sent_at >= grace
            ):
                _killpg(pgid, signal.SIGKILL)
                kill_sent = True

        # Nothing outlives the step: kill whatever is left in its process group.
        _killpg(pgid, signal.SIGKILL)

    wall = time.monotonic() - started
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    max_rss_kb = usage.ru_maxrss / 1024 if sys.platform == "darwin" else usage.ru_maxrss
    status.update(
        returncode=returncode,
        exit_code=returncode if returncode >= 0 else None,
        signal=-returncode if returncode < 0 else None,
        stop_reason=stop_reason,
        finished_at=_utc_now(),
        usage={
            "wall_seconds": round(wall, 3),
            "cpu_core_hours": round((usage.ru_utime + usage.ru_stime) / 3600.0, 6),
            "max_rss_mb": round(max_rss_kb / 1024.0, 3),
        },
    )
    _write_json_atomic(state_dir / "status.json", status)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(
            "usage: python -m orchestrator.executors._supervisor <state_dir>",
            file=sys.stderr,
        )
        return 2
    return run(Path(args[0]))


if __name__ == "__main__":
    raise SystemExit(main())
