"""The ``docker`` executor: one sibling container per step attempt (ADR-0003, card F0-14).

The worker never mounts the Docker socket. It talks to a restricted socket proxy
over TCP (``docker-socket-proxy`` service in ``docker-compose.yml``, see
docs/deployment.md) using a few Engine API calls through ``httpx``::

    GET    /images/{ref}/json                inspect (digest check, image ID)
    POST   /images/create                    only with ``pull_missing=True``
    POST   /containers/create, /start        create and start the attempt
    GET    /containers/{id}/json             poll
    POST   /containers/{id}/kill             SIGTERM on cancel/timeout, then SIGKILL
    GET    /containers/{id}/logs             log capture
    DELETE /containers/{id}                  remove after the final state is saved

What the proxy cannot do is look inside a ``containers/create`` body. That
restriction is this module's job: the body is built here from fixed templates
and the caller's request only contributes the image (checked against the
allow-list *before* any call to Docker), resources and the staged paths.

Container settings (step-contract.md sections 2 and 6): image run by content ID
after its digest was verified; fixed contract command (the image's ENTRYPOINT
and CMD are replaced); read-only root file system; ``/in`` and ``/ref/<name>``
read-only, ``/out`` and ``/tmp`` writable bind mounts of per-attempt host
directories; non-root ``--user``; ``--cap-drop ALL``; ``no-new-privileges``;
``--network none``; PID limit; CPU and memory limits (swap disabled); an init
process so that SIGTERM reaches the step.

Host paths. Bind-mount sources are paths on the *Docker host*. When the worker
itself runs in a container, ``host_path_map`` translates the worker's view of
the data directory (e.g. ``/app/data``) to the host's (e.g. ``/srv/data``).
Unmapped paths are used unchanged.

Persistence. Everything needed to find an attempt again is in the handle (container ID,
work directory, timeout) and in ``<work_dir>/.executor/``: ``request.json``,
``stop.json`` (a requested stop and its time) and ``status.json`` (the final
state). The first ``poll`` that sees the container exit writes ``status.json``,
copies the logs to ``<work_dir>/logs/step.log`` and removes the container, so
containers do not accumulate and ``poll``/``collect`` can be called from another
process after a worker restart (``restartable = True``). The SIGKILL that follows
an unanswered SIGTERM is sent by whichever ``poll`` runs after the grace period
(the orchestrator polls attempts it has not seen finish).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections import deque
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

from orchestrator.executors.base import (
    KILL_GRACE_SECONDS,
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
from orchestrator.executors.subprocess import (
    LOG_FILE,
    RESERVED_OUT,
    STATE_DIR,
    _read_json,
    _write_json_atomic,
    contract_environment,
)

CONTRACT_COMMAND = (
    "/usr/local/bin/run",
    "--in",
    "/in",
    "--out",
    "/out",
    "--params",
    "/in/params.json",
)
DEFAULT_PROXY_URL = "http://docker-proxy:2375"
DEFAULT_API_VERSION = "v1.41"
DEFAULT_PIDS_LIMIT = 512
FALLBACK_USER = "10001:10001"
LOG_LIMIT_BYTES = 10 * 1024 * 1024  # step-contract.md section 3, item 4
LABEL_PREFIX = "io.stroke-server."

_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_STAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?Z$")
_DIGEST = re.compile(r"@(sha256:[a-f0-9]{64})$")

ImageAllowList = Collection[str] | Callable[[str], bool]


class DockerAPIError(ExecutorError):
    """The Docker Engine API (through the proxy) answered with an error status."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"Docker API error {status}: {message}")
        self.status = status


def default_user() -> str:
    """The worker's own non-root ``uid:gid``, so that outputs stay readable and removable."""
    if hasattr(os, "getuid") and os.getuid() != 0:
        return f"{os.getuid()}:{os.getgid()}"
    return FALLBACK_USER


def parse_host_path_map(text: str) -> dict[str, str]:
    """Parse ``/worker/path=/host/path,/other=/elsewhere``."""
    mapping: dict[str, str] = {}
    for item in filter(None, (part.strip() for part in text.split(","))):
        worker, sep, host = item.partition("=")
        if not sep or not worker or not host:
            raise ValueError(f"invalid host path mapping: {item!r}")
        mapping[worker] = host
    return mapping


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _docker_stamp(value: str | None) -> str | None:
    """Engine timestamp (nanoseconds) to the contract's millisecond RFC 3339 form."""
    if not value:
        return None
    match = _STAMP.match(value)
    if not match or value.startswith("0001-"):
        return None
    return f"{match.group(1)}.{((match.group(2) or '') + '000')[:3]}Z"


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ"
    delta = datetime.strptime(end, fmt) - datetime.strptime(start, fmt)
    return max(delta.total_seconds(), 0.0)


# ------------------------------------------------------------------------- logs


class _LogSink:
    """Writes a step log, keeping the first and last halves beyond ``limit`` bytes."""

    def __init__(self, path: Path, limit: int = LOG_LIMIT_BYTES) -> None:
        self._file = path.open("wb")
        self._head_left = limit // 2
        self._tail_size = limit - limit // 2
        self._tail: deque[bytes] = deque()
        self._tail_len = 0
        self._dropped = 0

    def write(self, data: bytes) -> None:
        if self._head_left:
            head, data = data[: self._head_left], data[self._head_left :]
            self._file.write(head)
            self._head_left -= len(head)
        if not data:
            return
        self._tail.append(data)
        self._tail_len += len(data)
        while self._tail_len - len(self._tail[0]) >= self._tail_size:
            gone = self._tail.popleft()
            self._tail_len -= len(gone)
            self._dropped += len(gone)

    def close(self) -> None:
        tail = b"".join(self._tail)
        if len(tail) > self._tail_size:
            self._dropped += len(tail) - self._tail_size
            tail = tail[-self._tail_size :]
        if self._dropped:
            self._file.write(f"\n[... {self._dropped} bytes truncated ...]\n".encode())
        self._file.write(tail)
        self._file.close()


class _Demux:
    """Splits the Engine's multiplexed log stream (8-byte frame headers)."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> Iterator[bytes]:
        self._buffer.extend(data)
        while len(self._buffer) >= 8:
            size = int.from_bytes(self._buffer[4:8], "big")
            if len(self._buffer) < 8 + size:
                return
            payload = bytes(self._buffer[8 : 8 + size])
            del self._buffer[: 8 + size]
            yield payload


# ------------------------------------------------------------------------ client


class _Client:
    def __init__(
        self,
        base_url: str,
        api_version: str,
        timeout: float,
        transport: httpx.BaseTransport | None,
    ) -> None:
        self._http = httpx.Client(
            base_url=f"{base_url.rstrip('/')}/{api_version}",
            timeout=timeout,
            transport=transport,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Any = None,
        ok: Iterable[int] = (200, 201, 204),
    ) -> httpx.Response:
        try:
            response = self._http.request(method, path, params=params, json=body)
        except httpx.HTTPError as exc:
            raise ExecutorError(
                f"Docker socket proxy unreachable ({type(exc).__name__}: {exc})"
            ) from exc
        if response.status_code not in tuple(ok):
            raise DockerAPIError(response.status_code, _api_message(response))
        return response

    def stream_logs(self, container_id: str, sink: _LogSink) -> None:
        demux = _Demux()
        try:
            with self._http.stream(
                "GET",
                f"/containers/{container_id}/logs",
                params={"stdout": 1, "stderr": 1, "follow": 0},
            ) as response:
                if response.status_code != 200:
                    response.read()
                    raise DockerAPIError(response.status_code, _api_message(response))
                for chunk in response.iter_bytes():
                    for payload in demux.feed(chunk):
                        sink.write(payload)
        except httpx.HTTPError as exc:
            raise ExecutorError(f"log transfer failed ({type(exc).__name__})") from exc


def _api_message(response: httpx.Response) -> str:
    try:
        return str(response.json().get("message", response.text))
    except ValueError:
        return response.text[:200]


# ---------------------------------------------------------------------- executor


class DockerExecutor(Executor):
    """Run a step attempt as a sibling container through a restricted socket proxy.

    ``allowed_images`` is supplied by the caller (the pipeline registry): a
    collection of exact ``repository@sha256:<digest>`` references or a predicate.
    Anything else is refused before Docker is contacted.
    """

    name = "docker"
    runtime = "docker"
    restartable = True

    def __init__(
        self,
        *,
        allowed_images: ImageAllowList,
        base_url: str = DEFAULT_PROXY_URL,
        host_path_map: Mapping[str, str] | None = None,
        user: str | None = None,
        pids_limit: int = DEFAULT_PIDS_LIMIT,
        pull_missing: bool = False,
        allow_gpus: bool = False,
        kill_grace_seconds: float = KILL_GRACE_SECONDS,
        api_version: str = DEFAULT_API_VERSION,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._allowed = allowed_images
        self.host_path_map = {
            str(k).rstrip("/"): str(v).rstrip("/")
            for k, v in (host_path_map or {}).items()
        }
        self.user = user or default_user()
        if not re.fullmatch(r"[0-9]+:[0-9]+", self.user) or self.user.startswith("0:"):
            raise ValueError("user must be a non-root numeric 'uid:gid'")
        self.pids_limit = int(pids_limit)
        self.pull_missing = pull_missing
        self.allow_gpus = allow_gpus
        self.kill_grace_seconds = float(kill_grace_seconds)
        self._client = _Client(base_url, api_version, timeout, transport)

    @classmethod
    def from_env(
        cls,
        allowed_images: ImageAllowList,
        environ: Mapping[str, str] | None = None,
        **overrides: Any,
    ) -> DockerExecutor:
        """Build from ``DOCKER_PROXY_URL``, ``DOCKER_EXECUTOR_*`` variables."""
        env = os.environ if environ is None else environ
        values: dict[str, Any] = {
            "base_url": env.get("DOCKER_PROXY_URL", DEFAULT_PROXY_URL),
            "host_path_map": parse_host_path_map(
                env.get("DOCKER_EXECUTOR_HOST_PATH_MAP", "")
            ),
            "user": env.get("DOCKER_EXECUTOR_USER") or None,
            "pids_limit": int(
                env.get("DOCKER_EXECUTOR_PIDS_LIMIT", DEFAULT_PIDS_LIMIT)
            ),
            "pull_missing": _truthy(env.get("DOCKER_EXECUTOR_PULL_MISSING")),
            "allow_gpus": _truthy(env.get("DOCKER_EXECUTOR_ALLOW_GPUS")),
        }
        values.update(overrides)
        return cls(allowed_images=allowed_images, **values)

    # ----------------------------------------------------------------- policy

    def is_allowed(self, image: str) -> bool:
        if callable(self._allowed):
            return bool(self._allowed(image))
        return image in self._allowed

    def host_path(self, path: Path) -> str:
        """Translate a worker path to the path the Docker host sees."""
        text = path.resolve().as_posix()
        for prefix in sorted(self.host_path_map, key=len, reverse=True):
            if text == prefix or text.startswith(prefix + "/"):
                return self.host_path_map[prefix] + text[len(prefix) :]
        return text

    # ----------------------------------------------------------------- submit

    def submit(self, invocation: StepInvocation) -> ExecutorHandle:
        self.check_policy(invocation)
        image = invocation.image
        if image is None:
            raise ExecutorRefused("the docker executor requires a pinned image")
        if not self.is_allowed(image):
            raise ExecutorRefused(f"image is not on the allow-list: {image}")
        if invocation.resources.gpus > 0 and not self.allow_gpus:
            raise ExecutorRefused(
                "this docker executor is not configured to grant GPUs"
            )

        work = invocation.work_dir.resolve()
        if work.exists() and any(work.iterdir()):
            raise ExecutorError(
                "work_dir must be new or empty: every attempt starts clean"
            )
        image_id = self._resolve_image(image)

        paths = {n: work / n for n in ("in", "out", "tmp", "logs", STATE_DIR)}
        container_id: str | None = None
        try:
            for path in paths.values():
                path.mkdir(parents=True, exist_ok=True)
            self._stage(invocation, paths)
            _write_json_atomic(paths[STATE_DIR] / "request.json", invocation.to_dict())
            name = f"stepexec-{_NAME_UNSAFE.sub('-', invocation.step_run_id)[:80]}-a{invocation.attempt}"
            body = self.container_spec(invocation, image_id, paths, name)
            created = self._client.request(
                "POST", "/containers/create", params={"name": name}, body=body
            )
            container_id = created.json()["Id"]
            submitted_at = utc_now()
            self._client.request("POST", f"/containers/{container_id}/start")
        except BaseException:
            if container_id:
                self._remove(container_id)
            for path in paths.values():
                shutil.rmtree(path, ignore_errors=True)
            raise
        return ExecutorHandle(
            executor=self.name,
            external_job_id=container_id,
            submitted_at=submitted_at,
            data={
                "work_dir": str(work),
                "container_id": container_id,
                "container_name": name,
                "image_id": image_id,
                "timeout_seconds": invocation.effective_timeout_seconds,
            },
        )

    def _resolve_image(self, image: str) -> str:
        """Inspect the image and check that it really is the pinned digest."""
        digest = _DIGEST.search(image).group(1)  # shape guaranteed by StepInvocation
        response = self._client.request("GET", f"/images/{image}/json", ok=(200, 404))
        if response.status_code == 404:
            if not self.pull_missing:
                raise ExecutorRefused(
                    f"image {image} is not present on the Docker host and pulling is off"
                )
            self._client.request("POST", "/images/create", params={"fromImage": image})
            response = self._client.request("GET", f"/images/{image}/json")
        info = response.json()
        if not any(r.endswith("@" + digest) for r in info.get("RepoDigests") or []):
            raise ExecutorRefused(
                f"the local image does not carry the pinned digest {digest}"
            )
        return info["Id"]

    def _stage(self, invocation: StepInvocation, paths: Mapping[str, Path]) -> None:
        for name, source in invocation.inputs.items():
            if not source.is_file():
                raise ExecutorError(f"staged input {name!r} is not a file")
            target = paths["in"] / name
            shutil.copyfile(source, target)
            target.chmod(0o444)
        params = paths["in"] / "params.json"
        params.write_text(
            json.dumps(dict(invocation.params), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        params.chmod(0o444)
        for bundle in invocation.bundles:
            if not bundle.host_path.is_dir():
                raise ExecutorError(f"bundle {bundle.name!r} is not a directory")
        if self.user.split(":")[0] != str(getattr(os, "getuid", lambda: -1)()):
            # The container user differs from the worker: it must be able to write.
            paths["out"].chmod(0o777)
            paths["tmp"].chmod(0o777)

    def container_spec(
        self,
        invocation: StepInvocation,
        image_id: str,
        paths: Mapping[str, Path],
        name: str,
    ) -> dict[str, Any]:
        """The ``containers/create`` body (public for tests and documentation)."""

        def mount(source: Path, target: str, read_only: bool) -> dict[str, Any]:
            return {
                "Type": "bind",
                "Source": self.host_path(source),
                "Target": target,
                "ReadOnly": read_only,
                "BindOptions": {"CreateMountpoint": False},
            }

        mounts = [
            mount(paths["in"], "/in", True),
            *(mount(b.host_path, f"/ref/{b.name}", True) for b in invocation.bundles),
            mount(paths["out"], "/out", False),
            mount(paths["tmp"], "/tmp", False),
        ]
        env = contract_environment(invocation, PurePosixPath("/tmp"))  # type: ignore[arg-type]
        memory = invocation.resources.memory_mb * 1024 * 1024
        host_config: dict[str, Any] = {
            "Mounts": mounts,
            "ReadonlyRootfs": True,
            "NetworkMode": "none",
            "CapDrop": ["ALL"],
            "SecurityOpt": ["no-new-privileges"],
            "Privileged": False,
            "PidsLimit": self.pids_limit,
            "NanoCpus": invocation.resources.cpus * 1_000_000_000,
            "Memory": memory,
            "MemorySwap": memory,
            "Init": True,
            "AutoRemove": False,
            "RestartPolicy": {"Name": "no"},
            "LogConfig": {"Type": "json-file", "Config": {"max-size": "50m"}},
        }
        if invocation.resources.gpus > 0:
            host_config["DeviceRequests"] = [
                {
                    "Driver": "",
                    "Count": invocation.resources.gpus,
                    "Capabilities": [["gpu"]],
                }
            ]
        return {
            "Image": image_id,
            "Entrypoint": [CONTRACT_COMMAND[0]],
            "Cmd": list(CONTRACT_COMMAND[1:]),
            "User": self.user,
            "WorkingDir": "/tmp",
            "Env": [f"{k}={v}" for k, v in env.items()],
            "NetworkDisabled": True,
            "Tty": False,
            "OpenStdin": False,
            "AttachStdin": False,
            "StopTimeout": int(self.kill_grace_seconds),
            "Labels": {
                f"{LABEL_PREFIX}executor": "docker",
                f"{LABEL_PREFIX}step-run-id": invocation.step_run_id,
                f"{LABEL_PREFIX}attempt": str(invocation.attempt),
            },
            "HostConfig": host_config,
        }

    # ------------------------------------------------------------ poll/cancel

    def poll(self, handle: ExecutorHandle) -> JobStatus:
        status = self._finished_status(handle)
        if status is None:
            state = self._inspect(handle) or {}
            started = _docker_stamp(state.get("StartedAt"))
            if state.get("Status") == "created":
                return JobStatus(state=JobState.QUEUED)
            return JobStatus(state=JobState.RUNNING, started_at=started)
        outcome = classify_exit(status["exit_code"], stop_reason=status["stop_reason"])
        return JobStatus(
            state=JobState.FINISHED,
            outcome=outcome,
            exit_code=status["exit_code"],
            started_at=status.get("started_at"),
            finished_at=status.get("finished_at"),
            message=status.get("message"),
        )

    def cancel(self, handle: ExecutorHandle) -> bool:
        if self._finished_status(handle) is not None:
            return False
        stop = self._state_dir(handle) / "stop.json"
        if not stop.exists():
            self._request_stop(handle, "canceled")
        return True

    def _request_stop(self, handle: ExecutorHandle, reason: str) -> None:
        _write_json_atomic(
            self._state_dir(handle) / "stop.json",
            {"reason": reason, "requested_at": time.time(), "killed": False},
        )
        self._signal(handle, "SIGTERM")

    def _signal(self, handle: ExecutorHandle, name: str) -> None:
        # 404 or 409: the container is gone or already stopped; poll will notice.
        self._client.request(
            "POST",
            f"/containers/{handle.data['container_id']}/kill",
            params={"signal": name},
            ok=(204, 404, 409),
        )

    def _enforce_limits(self, handle: ExecutorHandle, state: Mapping[str, Any]) -> None:
        """Timeout (SIGTERM) and the SIGKILL that follows an unanswered SIGTERM."""
        stop_path = self._state_dir(handle) / "stop.json"
        stop = _read_json(stop_path)
        if stop is None:
            limit = int(handle.data.get("timeout_seconds") or 0)
            started = _docker_stamp(state.get("StartedAt"))
            if limit and started:
                elapsed = _seconds_between(started, utc_now())
                if elapsed is not None and elapsed > limit:
                    self._request_stop(handle, "timed_out")
            return
        if (
            not stop.get("killed")
            and time.time() - float(stop["requested_at"]) >= self.kill_grace_seconds
        ):
            self._signal(handle, "SIGKILL")
            stop["killed"] = True
            _write_json_atomic(stop_path, stop)

    # ------------------------------------------------------ state of the attempt

    def _inspect(self, handle: ExecutorHandle) -> dict[str, Any] | None:
        response = self._client.request(
            "GET", f"/containers/{handle.data['container_id']}/json", ok=(200, 404)
        )
        if response.status_code == 404:
            return None
        return response.json().get("State", {})

    def _finished_status(self, handle: ExecutorHandle) -> dict[str, Any] | None:
        """Final status if the attempt ended (saving it on first sight), else ``None``."""
        state_dir = self._state_dir(handle)
        saved = _read_json(state_dir / "status.json")
        if saved is not None:
            return saved
        state = self._inspect(handle)
        stop = _read_json(state_dir / "stop.json") or {}
        if state is None:  # container removed behind our back (host restart, operator)
            status = {
                "exit_code": None,
                "signal": None,
                "stop_reason": stop.get("reason"),
                "lost": True,
                "message": "The executor lost track of the container (host or Docker restart).",
                "started_at": None,
                "finished_at": utc_now(),
                "usage": {},
            }
        elif state.get("Status") in {"created", "running", "paused", "restarting"}:
            self._enforce_limits(handle, state)
            return None
        else:
            status = self._status_from_state(state, stop)
            self._save_logs(handle)
        _write_json_atomic(state_dir / "status.json", status)
        self._remove(handle.data["container_id"])
        return status

    @staticmethod
    def _status_from_state(
        state: Mapping[str, Any], stop: Mapping[str, Any]
    ) -> dict[str, Any]:
        raw = state.get("ExitCode")
        signal_number = None
        message = None
        if state.get("OOMKilled"):
            signal_number, message = (
                9,
                "The step was killed for exceeding its memory limit.",
            )
        elif isinstance(raw, int) and 129 <= raw <= 159:
            signal_number = raw - 128
            message = f"The step was killed by signal {signal_number}."
        reason = stop.get("reason")
        if reason == "timed_out":
            message = "The step exceeded its time limit and was stopped."
        elif reason == "canceled":
            message = "The step was canceled."
        elif state.get("Error"):
            message = f"The container could not run the step ({state['Error']})."
        started = _docker_stamp(state.get("StartedAt"))
        finished = _docker_stamp(state.get("FinishedAt")) or utc_now()
        wall = _seconds_between(started, finished)
        return {
            "exit_code": None if signal_number else raw,
            "docker_exit_code": raw,
            "signal": signal_number,
            "stop_reason": reason,
            "oom_killed": bool(state.get("OOMKilled")),
            "message": message,
            "started_at": started,
            "finished_at": finished,
            "usage": {} if wall is None else {"wall_seconds": wall},
        }

    def _save_logs(self, handle: ExecutorHandle) -> None:
        work = Path(handle.data["work_dir"])
        (work / "logs").mkdir(exist_ok=True)
        sink = _LogSink(work / "logs" / LOG_FILE)
        try:
            self._client.stream_logs(handle.data["container_id"], sink)
        except ExecutorError as exc:
            sink.write(f"\n[executor: log capture incomplete: {exc}]\n".encode())
        finally:
            sink.close()

    def _remove(self, container_id: str) -> None:
        try:
            self._client.request(
                "DELETE",
                f"/containers/{container_id}",
                params={"force": 1, "v": 1},
                ok=(204, 404),
            )
        except ExecutorError:
            pass  # a leftover stopped container is harmless; never mask the real error

    # -------------------------------------------------------- collect/cleanup

    def collect(self, handle: ExecutorHandle) -> StepResult:
        status = self._finished_status(handle)
        if status is None:
            raise ExecutorError("the attempt is still running; poll until it finishes")
        work = Path(handle.data["work_dir"])
        invocation = StepInvocation.from_dict(
            _read_json(work / STATE_DIR / "request.json") or {}
        )
        exit_code = status["exit_code"]
        outcome = classify_exit(exit_code, stop_reason=status["stop_reason"])
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
            external_job_id=handle.data["container_id"],
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
            message=status.get("message"),
        )

    def cleanup(self, handle: ExecutorHandle) -> None:
        if self._finished_status(handle) is None:
            raise ExecutorError("cannot clean up a running attempt; cancel it first")
        work = Path(handle.data["work_dir"])
        for name in ("in", "out", "tmp"):
            shutil.rmtree(work / name, ignore_errors=True)

    # ---------------------------------------------------------------- helpers

    def _state_dir(self, handle: ExecutorHandle) -> Path:
        if handle.executor != self.name:
            raise ExecutorError(f"handle belongs to executor {handle.executor!r}")
        return Path(handle.data["work_dir"]) / STATE_DIR
