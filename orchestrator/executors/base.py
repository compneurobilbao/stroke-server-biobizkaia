"""The executor interface and the data types it exchanges (ADR-0003, card F0-13).

An executor runs one *attempt* of one step on one backend. It does not know about
cases, pipelines or the database: the orchestrator (F0-15) stages inputs, chooses
the executor, persists the handle and decides what happens after the step ends.

Lifecycle::

    handle = executor.submit(invocation)   # start; returns quickly
    status = executor.poll(handle)         # repeatedly, until status.finished
    executor.cancel(handle)                # optional; SIGTERM, SIGKILL after 30 s
    result = executor.collect(handle)      # outcome, outputs, step records, usage
    executor.cleanup(handle)               # remove staging; logs are kept

Handles are plain JSON (``ExecutorHandle.to_dict`` / ``from_dict``) so that the
orchestrator can store them in ``StepRun`` and call ``poll``, ``cancel``,
``collect`` and ``cleanup`` from another process after a restart, where the
backend allows it (``Executor.restartable``).

Runtime rules applied by every backend: docs/contracts/step-contract.md sections
2 and 6. Records produced here: the ``execution`` block of
docs/contracts/provenance.md section 3.2 (``StepResult.execution_record``).
"""

from __future__ import annotations

import abc
import enum
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, ClassVar

# Exit codes (step-contract.md section 4.1)
EXIT_SUCCEEDED = 0
EXIT_REJECTED = 10

# Seconds between SIGTERM and SIGKILL on cancellation or timeout (step-contract.md 2.4)
KILL_GRACE_SECONDS = 30.0

# Variables every executor sets (step-contract.md section 2.3)
THREAD_VARIABLES = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS",
)

_SUBMITTED_BY = re.compile(r"^(user|service):[A-Za-z0-9-]{1,64}$")
_BILLING_ACCOUNT = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_IMAGE_DIGEST_REF = re.compile(
    r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+"
    r"@sha256:[a-f0-9]{64}$"
)
_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


class ExecutorError(RuntimeError):
    """An executor operation failed (backend unreachable, handle unknown, misuse)."""


class ExecutorRefused(ExecutorError):
    """The executor refuses the request before starting it (e.g. a GPU it cannot grant)."""


def utc_now() -> str:
    """RFC 3339 UTC timestamp with a literal ``Z`` and millisecond precision."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# --------------------------------------------------------------------------- policy


class SubmitAs(str, enum.Enum):
    """Identity under which jobs are submitted (provenance.md section 4).

    Only ``SERVICE_ACCOUNT`` is implemented in Phase 0 (ADR-0003, decision 5):
    every job runs under the platform's own service identity, and the platform
    user is recorded in ``Accounting.submitted_by``. ``PER_USER`` (jobs run under
    the researcher's own HPC account) and ``CHARGEBACK`` (service account, usage
    billed to the researcher's project) are declared so that the interface and
    the records do not change when they are built.
    """

    SERVICE_ACCOUNT = "service-account"
    PER_USER = "per-user"
    CHARGEBACK = "chargeback"


IMPLEMENTED_POLICIES: frozenset[SubmitAs] = frozenset({SubmitAs.SERVICE_ACCOUNT})


@dataclass(frozen=True)
class Accounting:
    """Who submitted a step run and who pays for it (same object as in RunProvenance)."""

    submitted_by: str
    submit_as: SubmitAs = SubmitAs.SERVICE_ACCOUNT
    billing_account: str | None = None

    def __post_init__(self) -> None:
        if not _SUBMITTED_BY.match(self.submitted_by):
            raise ValueError(
                "accounting.submitted_by must be 'user:<opaque id>' or 'service:<opaque id>'"
            )
        object.__setattr__(self, "submit_as", SubmitAs(self.submit_as))
        if self.billing_account is not None and not _BILLING_ACCOUNT.match(
            self.billing_account
        ):
            raise ValueError("accounting.billing_account has invalid characters")

    def to_dict(self) -> dict[str, Any]:
        return {
            "submitted_by": self.submitted_by,
            "submit_as": self.submit_as.value,
            "billing_account": self.billing_account,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Accounting:
        return cls(
            submitted_by=data["submitted_by"],
            submit_as=SubmitAs(data.get("submit_as", SubmitAs.SERVICE_ACCOUNT.value)),
            billing_account=data.get("billing_account"),
        )


def check_submit_policy(accounting: Accounting, executor_name: str) -> None:
    """Raise ``NotImplementedError`` for declared but unimplemented policies."""
    if accounting.submit_as in IMPLEMENTED_POLICIES:
        return
    raise NotImplementedError(
        f"submit_as '{accounting.submit_as.value}' is declared in the executor "
        f"interface but not implemented (executor '{executor_name}'); only "
        "'service-account' is available in this release (ADR-0003)."
    )


# ------------------------------------------------------------------------ request


@dataclass(frozen=True)
class Resources:
    """Resources granted to one attempt (step-contract.md 1.6, after deployment scaling)."""

    cpus: int
    memory_mb: int
    wall_time_minutes: int
    gpus: int = 0
    scratch_mb: int = 1024

    def __post_init__(self) -> None:
        if self.cpus < 1 or self.memory_mb < 1 or self.wall_time_minutes < 1:
            raise ValueError("cpus, memory_mb and wall_time_minutes must be >= 1")
        if self.gpus < 0 or self.scratch_mb < 0:
            raise ValueError("gpus and scratch_mb must be >= 0")

    def to_dict(self) -> dict[str, int]:
        return {
            "cpus": self.cpus,
            "memory_mb": self.memory_mb,
            "wall_time_minutes": self.wall_time_minutes,
            "gpus": self.gpus,
            "scratch_mb": self.scratch_mb,
        }

    def provenance_record(self) -> dict[str, int]:
        """The ``execution.resources`` object of the run provenance."""
        return {
            "cpus": self.cpus,
            "memory_mb": self.memory_mb,
            "gpus": self.gpus,
            "wall_time_minutes": self.wall_time_minutes,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Resources:
        return cls(**{k: int(v) for k, v in data.items()})


@dataclass(frozen=True)
class BundleMount:
    """A resolved, verified reference bundle mounted read-only at ``/ref/<name>/``."""

    name: str
    host_path: Path
    id: str | None = None
    version: str | None = None
    content_digest: str | None = None

    def __post_init__(self) -> None:
        if not _SNAKE.match(self.name):
            raise ValueError(f"bundle mount name must be snake_case: {self.name!r}")
        object.__setattr__(self, "host_path", Path(self.host_path))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "host_path": str(self.host_path),
            "id": self.id,
            "version": self.version,
            "content_digest": self.content_digest,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BundleMount:
        return cls(**data)


@dataclass(frozen=True)
class StepInvocation:
    """Everything an executor needs to run one attempt of one step.

    ``inputs`` maps the staged file name under ``/in`` (port name plus the
    artefact type's extension, e.g. ``ct.nii.gz``) to the host file the
    orchestrator resolved for it. ``params`` are the *effective* parameters,
    already validated; the executor writes them to ``/in/params.json``.

    ``image`` is the pinned ``repository@sha256:<digest>`` reference; it is
    REQUIRED by the docker and slurm backends and optional for subprocess.
    ``command`` overrides the contract command for backends that run local
    commands (subprocess); container backends always use the contract command.
    Placeholders ``{in_dir}``, ``{out_dir}``, ``{params}``, ``{ref_dir}`` and
    ``{tmp_dir}`` in its items are replaced by the backend's paths.

    ``timeout_seconds``: ``None`` means the granted wall time; ``0`` means no
    limit (legacy adapters only).

    ``env`` adds variables for legacy commands; contract variables
    (step-contract.md 2.3) always win. ``inherit_env`` lets a legacy command see
    the worker's environment minus secrets; contract steps leave it ``False``.
    """

    step_run_id: str
    step_name: str
    step_version: str
    resources: Resources
    accounting: Accounting
    work_dir: Path
    image: str | None = None
    inputs: Mapping[str, Path] = field(default_factory=dict)
    params: Mapping[str, Any] = field(default_factory=dict)
    bundles: Sequence[BundleMount] = ()
    timeout_seconds: int | None = None
    attempt: int = 1
    command: Sequence[str] | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    inherit_env: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "work_dir", Path(self.work_dir))
        object.__setattr__(
            self, "inputs", {str(k): Path(v) for k, v in dict(self.inputs).items()}
        )
        object.__setattr__(self, "bundles", tuple(self.bundles))
        if self.image is not None and not _IMAGE_DIGEST_REF.match(self.image):
            raise ValueError("image must be 'repository@sha256:<64 hex>'")
        if self.attempt < 1:
            raise ValueError("attempt is 1-based")
        if self.timeout_seconds is not None and self.timeout_seconds < 0:
            raise ValueError("timeout_seconds must be >= 0")
        for name in self.inputs:
            if (
                not name
                or "/" in name
                or "\\" in name
                or name in {".", "..", "params.json"}
            ):
                raise ValueError(f"invalid staged input name: {name!r}")
        names = [b.name for b in self.bundles]
        if len(names) != len(set(names)):
            raise ValueError("bundle mount names must be unique")

    @property
    def effective_timeout_seconds(self) -> int:
        """Timeout actually enforced; ``0`` means none."""
        if self.timeout_seconds is None:
            return self.resources.wall_time_minutes * 60
        return self.timeout_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_run_id": self.step_run_id,
            "step_name": self.step_name,
            "step_version": self.step_version,
            "resources": self.resources.to_dict(),
            "accounting": self.accounting.to_dict(),
            "work_dir": str(self.work_dir),
            "image": self.image,
            "inputs": {k: str(v) for k, v in self.inputs.items()},
            "params": dict(self.params),
            "bundles": [b.to_dict() for b in self.bundles],
            "timeout_seconds": self.timeout_seconds,
            "attempt": self.attempt,
            "command": list(self.command) if self.command is not None else None,
            "env": dict(self.env),
            "inherit_env": self.inherit_env,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StepInvocation:
        values = dict(data)
        values["resources"] = Resources.from_dict(values["resources"])
        values["accounting"] = Accounting.from_dict(values["accounting"])
        values["bundles"] = [
            BundleMount.from_dict(b) for b in values.get("bundles", [])
        ]
        return cls(**values)


# ------------------------------------------------------------------ state, result


class JobState(str, enum.Enum):
    QUEUED = "queued"  # accepted by the backend, not started (e.g. Slurm PENDING)
    RUNNING = "running"
    FINISHED = "finished"  # ended, for whatever reason; see JobStatus.outcome


class Outcome(str, enum.Enum):
    """Executor-level classification of how an attempt ended.

    ``TERMINATED`` means the process was killed by a signal the platform did not
    send (out-of-memory killer, operator, lost supervisor); the run provenance
    records it as ``failed``. ``contract_violation`` is decided by the
    orchestrator when it validates the collected files, not by executors.
    """

    SUCCEEDED = "succeeded"  # exit 0
    REJECTED = "rejected"  # exit 10
    FAILED = "failed"  # any other exit code
    TERMINATED = "terminated"  # killed by a signal not sent by the platform
    CANCELED = "canceled"  # stopped by Executor.cancel
    TIMED_OUT = "timed_out"  # stopped because the timeout expired

    @property
    def provenance_outcome(self) -> str:
        """Value of ``execution.outcome`` in RunProvenance (provenance.md 3.2)."""
        return "failed" if self is Outcome.TERMINATED else self.value

    @property
    def interrupted(self) -> bool:
        """``/out`` of an interrupted step is discarded (step-contract.md 2.4)."""
        return self in (Outcome.CANCELED, Outcome.TIMED_OUT)


def classify_exit(exit_code: int | None, *, stop_reason: str | None = None) -> Outcome:
    """Map a finished attempt to an outcome (step-contract.md 4.1).

    ``exit_code`` is ``None`` when the process was killed by a signal or its
    status is unknown. ``stop_reason`` is ``"canceled"`` or ``"timed_out"`` when
    the platform itself stopped the process; it takes precedence over the exit
    code, because a step that handles ``SIGTERM`` still exits by itself.
    """
    if stop_reason == "canceled":
        return Outcome.CANCELED
    if stop_reason == "timed_out":
        return Outcome.TIMED_OUT
    if stop_reason is not None:
        raise ValueError(f"unknown stop reason: {stop_reason!r}")
    if exit_code is None:
        return Outcome.TERMINATED
    if exit_code == EXIT_SUCCEEDED:
        return Outcome.SUCCEEDED
    if exit_code == EXIT_REJECTED:
        return Outcome.REJECTED
    return Outcome.FAILED


@dataclass(frozen=True)
class Usage:
    """Measured usage of one attempt (``execution.usage``); unknown values are ``None``."""

    wall_seconds: float | None = None
    cpu_core_hours: float | None = None
    gpu_hours: float | None = None
    max_rss_mb: float | None = None

    def to_dict(self) -> dict[str, float]:
        return {
            k: v
            for k, v in {
                "wall_seconds": self.wall_seconds,
                "cpu_core_hours": self.cpu_core_hours,
                "gpu_hours": self.gpu_hours,
                "max_rss_mb": self.max_rss_mb,
            }.items()
            if v is not None
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> Usage:
        return cls(**dict(data or {}))


@dataclass(frozen=True)
class ExecutorHandle:
    """Persistable reference to one submitted attempt.

    ``external_job_id`` is the backend's own ID (process ID, container ID, Slurm
    job ID). ``data`` holds whatever the backend needs to find the job again; it
    must be JSON-serialisable and must not contain secrets.
    """

    executor: str
    external_job_id: str
    submitted_at: str
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "executor": self.executor,
            "external_job_id": self.external_job_id,
            "submitted_at": self.submitted_at,
            "data": dict(self.data),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ExecutorHandle:
        return cls(
            executor=data["executor"],
            external_job_id=str(data["external_job_id"]),
            submitted_at=data["submitted_at"],
            data=dict(data.get("data", {})),
        )


@dataclass(frozen=True)
class JobStatus:
    state: JobState
    outcome: Outcome | None = None
    exit_code: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    message: str | None = None

    @property
    def finished(self) -> bool:
        return self.state is JobState.FINISHED


@dataclass(frozen=True)
class StepResult:
    """What ``collect`` returns.

    ``outputs`` maps file names found in ``/out`` (other than ``provenance.json``,
    ``error.json`` and ``extra/``) to host paths; it is empty for interrupted
    attempts. ``provenance`` and ``error`` are the step's own records parsed as
    JSON, verbatim, or ``None``; schema validation and hash checks are the
    orchestrator's job (provenance.md section 2).
    """

    outcome: Outcome
    exit_code: int | None
    signal: int | None
    out_dir: Path
    outputs: Mapping[str, Path]
    extra_dir: Path | None
    provenance: Mapping[str, Any] | None
    error: Mapping[str, Any] | None
    log_path: Path
    usage: Usage
    execution: Mapping[str, Any]
    message: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.outcome is Outcome.SUCCEEDED


def build_execution_record(
    *,
    executor: str,
    runtime: str,
    invocation: StepInvocation,
    outcome: Outcome,
    exit_code: int | None,
    queued_at: str | None,
    started_at: str | None,
    finished_at: str | None,
    usage: Usage,
    external_job_id: str | None = None,
    sif_sha256: str | None = None,
) -> dict[str, Any]:
    """The ``execution`` block of a step record (provenance.md 3.2)."""
    record: dict[str, Any] = {"executor": executor, "runtime": runtime}
    if invocation.image:
        record["image"] = invocation.image
    if sif_sha256:
        record["sif_sha256"] = sif_sha256
    if external_job_id:
        record["external_job_id"] = external_job_id
    record["attempt"] = invocation.attempt
    for key, value in (
        ("queued_at", queued_at),
        ("started_at", started_at),
        ("finished_at", finished_at),
    ):
        if value:
            record[key] = value
    record["exit_code"] = exit_code
    record["outcome"] = outcome.provenance_outcome
    record["resources"] = invocation.resources.provenance_record()
    usage_record = usage.to_dict()
    if usage_record:
        record["usage"] = usage_record
    return record


# ----------------------------------------------------------------------- interface


class Executor(abc.ABC):
    """One backend that can run step attempts.

    Implementations must be safe to call from a different process than the one
    that called ``submit`` when ``restartable`` is true: every method takes the
    persisted handle and must not depend on in-memory state.
    """

    name: ClassVar[str]
    runtime: ClassVar[str]
    restartable: ClassVar[bool] = False

    def check_policy(self, invocation: StepInvocation) -> None:
        check_submit_policy(invocation.accounting, self.name)

    @abc.abstractmethod
    def submit(self, invocation: StepInvocation) -> ExecutorHandle:
        """Stage and start the attempt; return without waiting for it to finish.

        Raises ``NotImplementedError`` for unimplemented ``submit_as`` policies
        and ``ExecutorRefused`` when the request cannot be granted.
        """

    @abc.abstractmethod
    def poll(self, handle: ExecutorHandle) -> JobStatus:
        """Current state; enforces nothing that requires polling to be frequent."""

    @abc.abstractmethod
    def cancel(self, handle: ExecutorHandle) -> bool:
        """Request termination (SIGTERM, then SIGKILL after 30 s).

        Returns ``True`` if a running or queued attempt was asked to stop,
        ``False`` if it had already finished. Does not wait; poll afterwards.
        """

    @abc.abstractmethod
    def collect(self, handle: ExecutorHandle) -> StepResult:
        """Outcome, outputs and records of a finished attempt (idempotent)."""

    @abc.abstractmethod
    def cleanup(self, handle: ExecutorHandle) -> None:
        """Remove staging and scratch space; keep logs. Idempotent."""

    def wait(
        self,
        handle: ExecutorHandle,
        *,
        poll_interval: float = 1.0,
        timeout: float | None = None,
    ) -> JobStatus:
        """Poll until the attempt finishes (convenience for tests and legacy paths)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            status = self.poll(handle)
            if status.finished:
                return status
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("attempt still running")
            time.sleep(poll_interval)
