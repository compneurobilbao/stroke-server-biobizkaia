"""The ``subprocess`` executor: a local command on the worker host (POSIX only).

Used for steps installed locally (e.g. an SDK step run outside its image) and for
the legacy dummy and anatproc adapters during migration (ADR-0003). There is no
container, so the contract paths are per-attempt host directories::

    <work_dir>/in/       staged inputs and params.json      (container: /in)
    <work_dir>/out/      empty at start                      (container: /out)
    <work_dir>/ref/<n>   symlinks to resolved bundles         (container: /ref/<n>)
    <work_dir>/tmp/      scratch; HOME and TMPDIR point here  (container: /tmp)
    <work_dir>/logs/     step.log (stdout+stderr in order), supervisor.log
    <work_dir>/.executor/  spec, request, supervisor, child and status records

Isolation that a container gives (read-only root, no network, arbitrary UID,
memory limit) is *not* enforced here; granted memory is recorded and exported as
``STEP_MEMORY_MB`` only. Steps that need those guarantees run under the docker or
slurm executors.

Each attempt runs under a small supervisor process (``_supervisor``) started in
its own session. The supervisor starts the command in a new process group,
enforces the timeout, stops the whole group with SIGTERM then SIGKILL after
30 seconds, and writes ``status.json``. Because state lives in files, ``poll``,
``cancel``, ``collect`` and ``cleanup`` work from a persisted handle in another
process (``restartable = True``) as long as (a) ``work_dir`` is on storage that
survives the restart and (b) the supervisor survived it, which is the case when
only the worker process restarts. If the host or container restarts, the
attempt dies with it; ``poll`` then reports it finished with outcome
``terminated`` (``canceled`` if a cancel had been requested) and the
orchestrator retries per its policy.

``adopt`` finds an attempt whose handle was never recorded: ``request.json``
identifies the attempt and the supervisor records its own PID and start time in
``supervisor.json`` before doing anything else.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from orchestrator.executors.base import (
    KILL_GRACE_SECONDS,
    THREAD_VARIABLES,
    Executor,
    ExecutorError,
    ExecutorHandle,
    ExecutorRefused,
    JobState,
    JobStatus,
    StepInvocation,
    StepResult,
    Usage,
    build_execution_record,
    classify_exit,
    utc_now,
)

CONTRACT_RUN = "/usr/local/bin/run"
CONTRACT_COMMAND = (
    "{run}",
    "--in",
    "{in_dir}",
    "--out",
    "{out_dir}",
    "--params",
    "{params}",
)
STATE_DIR = ".executor"
LOG_FILE = "step.log"
RESERVED_OUT = {"provenance.json", "error.json", "extra"}

# Worker variables never passed to legacy commands, even with inherit_env.
SECRET_ENV = re.compile(
    r"(PASSWORD|SECRET|TOKEN|CREDENTIAL|PRIVATE|_KEY$|^DATABASE_URL$|^CELERY_|^SMTP_|^AUTH_|^REDIS_)",
    re.IGNORECASE,
)

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def contract_environment(invocation: StepInvocation, tmp_dir: Path) -> dict[str, str]:
    """Variables required by step-contract.md section 2.3."""
    cpus = str(invocation.resources.cpus)
    env = {
        "HOME": str(tmp_dir),
        "TMPDIR": str(tmp_dir),
        "TZ": "UTC",
        "STEP_CPUS": cpus,
        "STEP_MEMORY_MB": str(invocation.resources.memory_mb),
        "STEP_GPUS": str(invocation.resources.gpus),
    }
    env.update({name: cpus for name in THREAD_VARIABLES})
    return env


def _proc_start_ticks(pid: int) -> int | None:
    """Process start time from /proc (guards against PID reuse); ``None`` if unknown."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    fields = raw.rsplit(")", 1)[-1].split()
    try:
        return int(fields[19])
    except (IndexError, ValueError):
        return None


def process_alive(pid: int, start_ticks: int | None = None) -> bool:
    """True if ``pid`` is running (not a zombie) and, if given, started at ``start_ticks``."""
    stat = Path(f"/proc/{pid}/stat")
    if Path("/proc/self/stat").exists():
        try:
            raw = stat.read_text(encoding="ascii", errors="replace")
        except OSError:
            return False
        fields = raw.rsplit(")", 1)[-1].split()
        if not fields or fields[0] in {"Z", "X", "x"}:
            return False
        if start_ticks is not None:
            try:
                return int(fields[19]) == start_ticks
            except (IndexError, ValueError):
                return True
        return True
    try:  # POSIX without /proc (macOS); zombies are not detected here
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: Path, data: Mapping[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


class SubprocessExecutor(Executor):
    """Run a step attempt as a local process group.

    ``passthrough_env`` names worker variables copied into the clean environment
    (the subprocess equivalent of "the image's own variables"); ``PATH`` is
    needed to find interpreters. ``run_executable`` replaces ``/usr/local/bin/run``
    in the contract command when the invocation has no ``command``.
    """

    name = "subprocess"
    runtime = "none"
    restartable = True

    def __init__(
        self,
        *,
        passthrough_env: Sequence[str] = ("PATH",),
        run_executable: str = CONTRACT_RUN,
        kill_grace_seconds: float = KILL_GRACE_SECONDS,
        python_executable: str | None = None,
    ) -> None:
        self.passthrough_env = tuple(passthrough_env)
        self.run_executable = run_executable
        self.kill_grace_seconds = float(kill_grace_seconds)
        self.python_executable = python_executable or sys.executable
        # Supervisors started by this process, so that they can be reaped.
        self._children: dict[int, subprocess.Popen] = {}

    # ----------------------------------------------------------------- submit

    def submit(self, invocation: StepInvocation) -> ExecutorHandle:
        self.check_policy(invocation)
        if os.name != "posix":
            raise ExecutorRefused("the subprocess executor requires a POSIX host")
        if invocation.resources.gpus > 0:
            raise ExecutorRefused("the subprocess executor cannot grant GPUs")

        work = invocation.work_dir.resolve()
        if work.exists() and any(work.iterdir()):
            raise ExecutorError(
                "work_dir must be new or empty: every attempt starts clean"
            )
        paths = {
            name: work / name for name in ("in", "out", "tmp", "ref", "logs", STATE_DIR)
        }
        for path in paths.values():
            path.mkdir(parents=True, exist_ok=True)

        self._stage(invocation, paths["in"], paths["ref"])
        params_path = paths["in"] / "params.json"

        mapping = {
            "{run}": self.run_executable,
            "{in_dir}": str(paths["in"]),
            "{out_dir}": str(paths["out"]),
            "{params}": str(params_path),
            "{ref_dir}": str(paths["ref"]),
            "{tmp_dir}": str(paths["tmp"]),
        }
        template = (
            invocation.command if invocation.command is not None else CONTRACT_COMMAND
        )
        argv = [_substitute(item, mapping) for item in template]
        if not argv:
            raise ExecutorError("empty command")

        log_path = paths["logs"] / LOG_FILE
        log_path.touch()
        state = paths[STATE_DIR]
        _write_json_atomic(state / "request.json", invocation.to_dict())
        _write_json_atomic(
            state / "spec.json",
            {
                "argv": argv,
                "cwd": str(paths["tmp"]),
                "env": self._environment(invocation, paths["tmp"]),
                "log_path": str(log_path),
                "timeout_seconds": invocation.effective_timeout_seconds,
                "kill_grace_seconds": self.kill_grace_seconds,
            },
        )

        submitted_at = utc_now()
        with (paths["logs"] / "supervisor.log").open("ab") as sup_log:
            proc = subprocess.Popen(
                [
                    self.python_executable,
                    "-m",
                    "orchestrator.executors._supervisor",
                    str(state),
                ],
                cwd=str(state),
                env={
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                    "PYTHONPATH": str(_PACKAGE_ROOT),
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                stdin=subprocess.DEVNULL,
                stdout=sup_log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                close_fds=True,
            )
        self._children[proc.pid] = proc
        return ExecutorHandle(
            executor=self.name,
            external_job_id=str(proc.pid),
            submitted_at=submitted_at,
            data={
                "work_dir": str(work),
                "supervisor_pid": proc.pid,
                "supervisor_start_ticks": _proc_start_ticks(proc.pid),
            },
        )

    def _stage(self, invocation: StepInvocation, in_dir: Path, ref_dir: Path) -> None:
        for name, source in invocation.inputs.items():
            if not source.is_file():
                raise ExecutorError(f"staged input {name!r} is not a file")
            target = in_dir / name
            shutil.copyfile(source, target)
            target.chmod(0o444)
        params_path = in_dir / "params.json"
        params_path.write_text(
            json.dumps(dict(invocation.params), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        params_path.chmod(0o444)
        for bundle in invocation.bundles:
            if not bundle.host_path.is_dir():
                raise ExecutorError(f"bundle {bundle.name!r} is not a directory")
            (ref_dir / bundle.name).symlink_to(
                bundle.host_path.resolve(), target_is_directory=True
            )

    def _environment(self, invocation: StepInvocation, tmp_dir: Path) -> dict[str, str]:
        if invocation.inherit_env:
            env = {k: v for k, v in os.environ.items() if not SECRET_ENV.search(k)}
        else:
            env = {k: os.environ[k] for k in self.passthrough_env if k in os.environ}
        env.update({str(k): str(v) for k, v in invocation.env.items()})
        env.update(contract_environment(invocation, tmp_dir))
        return env

    # ------------------------------------------------------------ poll/cancel

    def poll(self, handle: ExecutorHandle) -> JobStatus:
        state = self._state_dir(handle)
        status = self._finished_status(handle)
        if status is None:
            child = _read_json(state / "child.json") or {}
            return JobStatus(state=JobState.RUNNING, started_at=child.get("started_at"))
        stop_reason = status.get("stop_reason")
        outcome = classify_exit(status.get("exit_code"), stop_reason=stop_reason)
        return JobStatus(
            state=JobState.FINISHED,
            outcome=outcome,
            exit_code=status.get("exit_code"),
            started_at=status.get("started_at"),
            finished_at=status.get("finished_at"),
            message=_status_message(status),
        )

    def _finished_status(self, handle: ExecutorHandle) -> dict[str, Any] | None:
        """``status.json`` if the attempt ended; writes one if the supervisor was lost."""
        state = self._state_dir(handle)
        status = _read_json(state / "status.json")
        pid = int(handle.data["supervisor_pid"])
        if status is not None:
            self._reap(pid)
            return status
        if self._supervisor_alive(handle):
            return None
        status = _read_json(state / "status.json")  # written just before it exited
        if status is not None:
            return status
        # The supervisor disappeared without a status (host restart, SIGKILL).
        child = _read_json(state / "child.json") or {}
        if child.get("pgid"):
            _killpg(int(child["pgid"]), signal.SIGKILL)
        status = {
            "returncode": None,
            "exit_code": None,
            "signal": None,
            "stop_reason": "canceled"
            if (state / "cancel_requested").exists()
            else None,
            "lost": True,
            "started_at": child.get("started_at"),
            "finished_at": utc_now(),
            "usage": {},
        }
        _write_json_atomic(state / "status.json", status)
        return status

    def _supervisor_alive(self, handle: ExecutorHandle) -> bool:
        pid = int(handle.data["supervisor_pid"])
        if pid <= 0:  # adopted attempt whose supervisor never recorded itself
            return False
        proc = self._children.get(pid)
        if proc is not None:
            return proc.poll() is None
        return process_alive(pid, handle.data.get("supervisor_start_ticks"))

    def _reap(self, pid: int) -> None:
        proc = self._children.pop(pid, None)
        if proc is not None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._children[pid] = proc

    def cancel(self, handle: ExecutorHandle) -> bool:
        state = self._state_dir(handle)
        if (state / "status.json").exists():
            return False
        (state / "cancel_requested").write_text(utc_now(), encoding="utf-8")
        if self._supervisor_alive(handle):
            try:
                os.kill(int(handle.data["supervisor_pid"]), signal.SIGTERM)
            except ProcessLookupError:
                pass
        else:
            self._finished_status(handle)  # kills an orphaned group, records the cancel
        return True

    # -------------------------------------------------------- collect/cleanup

    def collect(self, handle: ExecutorHandle) -> StepResult:
        status = self._finished_status(handle)
        if status is None:
            raise ExecutorError("the attempt is still running; poll until it finishes")
        work = Path(handle.data["work_dir"])
        invocation = StepInvocation.from_dict(
            _read_json(work / STATE_DIR / "request.json") or {}
        )
        exit_code = status.get("exit_code")
        outcome = classify_exit(exit_code, stop_reason=status.get("stop_reason"))
        usage = Usage.from_dict(status.get("usage"))
        out_dir = work / "out"

        outputs: dict[str, Path] = {}
        provenance = error = None
        extra_dir = None
        if not outcome.interrupted and out_dir.is_dir():
            for entry in sorted(out_dir.iterdir()):
                if entry.name not in RESERVED_OUT and entry.is_file():
                    outputs[entry.name] = entry
            provenance = _read_json(out_dir / "provenance.json")
            error = _read_json(out_dir / "error.json")
            if (out_dir / "extra").is_dir():
                extra_dir = out_dir / "extra"

        execution = build_execution_record(
            executor=self.name,
            runtime=self.runtime,
            invocation=invocation,
            outcome=outcome,
            exit_code=exit_code,
            queued_at=handle.submitted_at,
            started_at=status.get("started_at"),
            finished_at=status.get("finished_at"),
            usage=usage,
        )
        return StepResult(
            outcome=outcome,
            exit_code=exit_code,
            signal=status.get("signal"),
            out_dir=out_dir,
            outputs=outputs,
            extra_dir=extra_dir,
            provenance=provenance,
            error=error,
            log_path=work / "logs" / LOG_FILE,
            usage=usage,
            execution=execution,
            message=_status_message(status),
        )

    def cleanup(self, handle: ExecutorHandle) -> None:
        if self._finished_status(handle) is None:
            raise ExecutorError("cannot clean up a running attempt; cancel it first")
        _remove_staging(Path(handle.data["work_dir"]), ("in", "out", "tmp", "ref"))

    # ------------------------------------------------------------------ adopt

    def adopt(self, invocation: StepInvocation) -> ExecutorHandle | None:
        """Find the supervisor an interrupted ``submit`` started (see ``Executor.adopt``).

        ``submit`` writes ``request.json`` before it starts the supervisor, and
        the supervisor writes ``supervisor.json`` before anything else. Without
        either record no step process was ever started.
        """
        work = invocation.work_dir.resolve()
        state = work / STATE_DIR
        if not owns_work_dir(state, invocation):
            _remove_staging(work, ("in", "out", "tmp", "ref"))
            return None
        supervisor = _read_json(state / "supervisor.json")
        status = _read_json(state / "status.json")
        if supervisor is None and status is None:
            _remove_staging(work, ("in", "out", "tmp", "ref"))
            return None
        supervisor = supervisor or {}
        pid = int(supervisor.get("pid", -1))
        return ExecutorHandle(
            executor=self.name,
            external_job_id=str(pid),
            submitted_at=supervisor.get("started_at")
            or (status or {}).get("started_at")
            or utc_now(),
            data={
                "work_dir": str(work),
                "supervisor_pid": pid,
                "supervisor_start_ticks": supervisor.get("start_ticks"),
            },
        )

    # ---------------------------------------------------------------- helpers

    def _state_dir(self, handle: ExecutorHandle) -> Path:
        if handle.executor != self.name:
            raise ExecutorError(f"handle belongs to executor {handle.executor!r}")
        return Path(handle.data["work_dir"]) / STATE_DIR


def owns_work_dir(state_dir: Path, invocation: StepInvocation) -> bool:
    """Whether ``state_dir`` holds the request of exactly this attempt.

    ``False`` if no request was recorded (the submission stopped before it, so
    nothing was started). Raises ``ExecutorRefused`` if the recorded request is
    unreadable or names another step run or attempt: such a directory is not
    touched.
    """
    path = state_dir / "request.json"
    if not path.exists():
        return False
    request = _read_json(path)
    if not isinstance(request, dict):
        raise ExecutorRefused("the work directory holds an unreadable request")
    if (request.get("step_run_id"), request.get("attempt")) != (
        invocation.step_run_id,
        invocation.attempt,
    ):
        raise ExecutorRefused("the work directory belongs to another attempt")
    return True


def _remove_staging(work: Path, names: Sequence[str]) -> None:
    for name in names:
        path = work / name
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def _substitute(item: str, mapping: Mapping[str, str]) -> str:
    for key, value in mapping.items():
        item = item.replace(key, value)
    return item


def _killpg(pgid: int, signum: int) -> None:
    try:
        os.killpg(pgid, signum)
    except (ProcessLookupError, PermissionError):
        pass


def _status_message(status: Mapping[str, Any]) -> str | None:
    if status.get("lost"):
        return "The executor lost track of the process (host or worker restart)."
    if status.get("spawn_error"):
        return f"The command could not be started ({status['spawn_error']})."
    if status.get("stop_reason") == "timed_out":
        return "The step exceeded its time limit and was stopped."
    if status.get("stop_reason") == "canceled":
        return "The step was canceled."
    if status.get("signal"):
        return f"The step was killed by signal {status['signal']}."
    return None
