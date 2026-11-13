"""Unit tests of the docker executor against a fake Docker Engine API (no daemon needed).

The fake answers the few calls the executor makes through ``httpx.MockTransport``.
The end-to-end test against a real daemon is tests/test_docker_executor_integration.py.
"""

from __future__ import annotations

import json
import os
import re
import struct
from pathlib import Path

import httpx
import jsonschema
import pytest

from orchestrator.executors import (
    Accounting,
    BundleMount,
    DockerExecutor,
    ExecutorError,
    ExecutorHandle,
    ExecutorRefused,
    JobState,
    Outcome,
    Resources,
    StepInvocation,
    SubmitAs,
)
from orchestrator.executors.base import utc_now
from orchestrator.executors.docker import (
    _Demux,
    _LogSink,
    parse_host_path_map,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DIGEST = "a" * 64
IMAGE = f"registry.example.org/steps/stub@sha256:{DIGEST}"
IMAGE_ID = "sha256:" + "b" * 64
CONTAINER_ID = "c" * 64


def frame(stream: int, text: str) -> bytes:
    data = text.encode()
    return struct.pack(">BxxxI", stream, len(data)) + data


class FakeDocker:
    """A minimal Engine: one image, containers with a settable state."""

    def __init__(self, *, repo_digests=None, image_present=True) -> None:
        self.calls: list[tuple[str, str]] = []
        self.bodies: dict[str, dict] = {}
        self.repo_digests = (
            [f"registry.example.org/steps/stub@sha256:{DIGEST}"]
            if repo_digests is None
            else repo_digests
        )
        self.image_present = image_present
        self.containers: dict[str, dict] = {}
        self.logs = frame(1, "INFO hello\n") + frame(2, "WARNING careful\n")
        self.signals: list[str] = []

    def finish(
        self, exit_code=0, *, oom=False, started="2026-11-13T10:00:00.123456789Z"
    ):
        self.containers[CONTAINER_ID]["State"] = {
            "Status": "exited",
            "Running": False,
            "ExitCode": exit_code,
            "OOMKilled": oom,
            "StartedAt": started,
            "FinishedAt": "2026-11-13T10:00:07.623456789Z",
            "Error": "",
        }

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = re.sub(r"^/v[0-9.]+", "", request.url.path)
        self.calls.append((request.method, path))
        method = request.method
        if method == "GET" and path.startswith("/images/") and path.endswith("/json"):
            if not self.image_present:
                return httpx.Response(404, json={"message": "no such image"})
            return httpx.Response(
                200, json={"Id": IMAGE_ID, "RepoDigests": self.repo_digests}
            )
        if method == "POST" and path == "/images/create":
            self.image_present = True
            return httpx.Response(200, text='{"status":"Pull complete"}\n')
        if method == "POST" and path == "/containers/create":
            body = json.loads(request.content)
            self.bodies["create"] = body
            self.bodies["create_name"] = request.url.params["name"]
            self.containers[CONTAINER_ID] = {
                "State": {"Status": "created", "Running": False}
            }
            return httpx.Response(201, json={"Id": CONTAINER_ID})
        if method == "POST" and path == f"/containers/{CONTAINER_ID}/start":
            self.containers[CONTAINER_ID]["State"] = {
                "Status": "running",
                "Running": True,
                "StartedAt": utc_now()[:-1] + "000000Z",
            }
            return httpx.Response(204)
        if method == "GET" and path == f"/containers/{CONTAINER_ID}/json":
            if CONTAINER_ID not in self.containers:
                return httpx.Response(404, json={"message": "no such container"})
            return httpx.Response(200, json=self.containers[CONTAINER_ID])
        if method == "POST" and path == f"/containers/{CONTAINER_ID}/kill":
            self.signals.append(request.url.params["signal"])
            return httpx.Response(204)
        if method == "GET" and path == f"/containers/{CONTAINER_ID}/logs":
            return httpx.Response(200, content=self.logs)
        if method == "DELETE" and path == f"/containers/{CONTAINER_ID}":
            self.containers.pop(CONTAINER_ID, None)
            return httpx.Response(204)
        return httpx.Response(500, json={"message": f"unexpected {method} {path}"})


@pytest.fixture()
def docker():
    return FakeDocker()


def make_executor(docker: FakeDocker, **overrides) -> DockerExecutor:
    values = dict(
        allowed_images=[IMAGE],
        base_url="http://proxy.invalid:2375",
        user="10001:10001",
        transport=docker.transport(),
        kill_grace_seconds=30,
    )
    values.update(overrides)
    return DockerExecutor(**values)


def invocation(tmp_path: Path, **overrides) -> StepInvocation:
    tmp_path.mkdir(parents=True, exist_ok=True)
    source = tmp_path / "source.nii.gz"
    source.write_bytes(b"x")
    values = dict(
        step_run_id="sr-1",
        step_name="stub",
        step_version="1.0.0",
        resources=Resources(cpus=2, memory_mb=512, wall_time_minutes=5),
        accounting=Accounting(submitted_by="user:8d2e6b1a"),
        work_dir=tmp_path / "attempt-1",
        image=IMAGE,
        inputs={"lesion_mask.nii.gz": source},
        params={"seed": 7},
    )
    values.update(overrides)
    return StepInvocation(**values)


def execution_validator():
    full = json.loads(
        (REPO_ROOT / "contracts/schemas/v1/provenance.schema.json").read_text(
            encoding="utf-8"
        )
    )
    schema = {
        "$schema": full["$schema"],
        "$defs": full["$defs"],
        "$ref": "#/$defs/execution",
    }
    return jsonschema.Draft202012Validator(schema)


# ------------------------------------------------------------------ interface


def test_declares_restartable_docker_backend():
    assert DockerExecutor.name == "docker"
    assert DockerExecutor.runtime == "docker"
    assert DockerExecutor.restartable is True
    assert not DockerExecutor.__abstractmethods__


# --------------------------------------------------------------------- refusal


def test_image_not_on_allow_list_is_refused_before_contacting_docker(tmp_path, docker):
    other = f"registry.example.org/steps/other@sha256:{'d' * 64}"
    executor = make_executor(docker)
    with pytest.raises(ExecutorRefused, match="allow-list"):
        executor.submit(invocation(tmp_path, image=other))
    assert docker.calls == []
    assert not (tmp_path / "attempt-1").exists()


def test_missing_image_is_refused_before_contacting_docker(tmp_path, docker):
    with pytest.raises(ExecutorRefused, match="pinned image"):
        make_executor(docker).submit(invocation(tmp_path, image=None))
    assert docker.calls == []


def test_allow_list_may_be_a_predicate(tmp_path, docker):
    seen = []
    executor = make_executor(
        docker, allowed_images=lambda ref: seen.append(ref) or False
    )
    with pytest.raises(ExecutorRefused):
        executor.submit(invocation(tmp_path))
    assert seen == [IMAGE]
    assert docker.calls == []


def test_unimplemented_submit_policy_is_refused_before_docker(tmp_path, docker):
    inv = invocation(
        tmp_path,
        accounting=Accounting(submitted_by="user:1", submit_as=SubmitAs.PER_USER),
    )
    with pytest.raises(NotImplementedError):
        make_executor(docker).submit(inv)
    assert docker.calls == []


def test_gpus_refused_unless_enabled(tmp_path, docker):
    inv = invocation(
        tmp_path, resources=Resources(cpus=1, memory_mb=64, wall_time_minutes=1, gpus=1)
    )
    with pytest.raises(ExecutorRefused, match="GPU"):
        make_executor(docker).submit(inv)
    assert docker.calls == []


def test_gpus_requested_through_device_requests_when_enabled(tmp_path, docker):
    inv = invocation(
        tmp_path, resources=Resources(cpus=1, memory_mb=64, wall_time_minutes=1, gpus=2)
    )
    make_executor(docker, allow_gpus=True).submit(inv)
    requests = docker.bodies["create"]["HostConfig"]["DeviceRequests"]
    assert requests[0]["Count"] == 2 and requests[0]["Capabilities"] == [["gpu"]]


def test_local_image_with_another_digest_is_refused(tmp_path, docker):
    docker.repo_digests = [f"registry.example.org/steps/stub@sha256:{'e' * 64}"]
    with pytest.raises(ExecutorRefused, match="pinned digest"):
        make_executor(docker).submit(invocation(tmp_path))
    assert ("POST", "/containers/create") not in docker.calls


def test_absent_image_is_refused_unless_pulling_is_enabled(tmp_path, docker):
    docker.image_present = False
    with pytest.raises(ExecutorRefused, match="not present"):
        make_executor(docker).submit(invocation(tmp_path))
    assert ("POST", "/images/create") not in docker.calls

    make_executor(docker, pull_missing=True).submit(invocation(tmp_path))
    assert ("POST", "/images/create") in docker.calls


def test_non_root_user_is_required(docker):
    with pytest.raises(ValueError):
        make_executor(docker, user="0:0")
    with pytest.raises(ValueError):
        make_executor(docker, user="root")


def test_unreachable_proxy_is_an_executor_error(tmp_path):
    def boom(request):
        raise httpx.ConnectError("refused")

    executor = DockerExecutor(
        allowed_images=[IMAGE],
        user="10001:10001",
        transport=httpx.MockTransport(boom),
    )
    with pytest.raises(ExecutorError, match="unreachable"):
        executor.submit(invocation(tmp_path))


# ------------------------------------------------------------- container spec


def test_container_spec_applies_the_contract_constraints(tmp_path, docker):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    inv = invocation(
        tmp_path,
        bundles=[BundleMount(name="atlas", host_path=bundle_dir)],
        timeout_seconds=90,
    )
    executor = make_executor(docker, pids_limit=64)
    handle = executor.submit(inv)

    body = docker.bodies["create"]
    host = body["HostConfig"]
    assert body["Image"] == IMAGE_ID  # run by verified content ID
    assert body["Entrypoint"] == ["/usr/local/bin/run"]
    assert body["Cmd"] == [
        "--in",
        "/in",
        "--out",
        "/out",
        "--params",
        "/in/params.json",
    ]
    assert body["User"] == "10001:10001"
    assert body["NetworkDisabled"] is True and host["NetworkMode"] == "none"
    assert host["ReadonlyRootfs"] is True
    assert host["CapDrop"] == ["ALL"]
    assert host["SecurityOpt"] == ["no-new-privileges"]
    assert host["Privileged"] is False
    assert host["PidsLimit"] == 64
    assert host["NanoCpus"] == 2_000_000_000
    assert host["Memory"] == host["MemorySwap"] == 512 * 1024 * 1024
    assert host["AutoRemove"] is False and host["Init"] is True
    assert "Binds" not in host and "DeviceRequests" not in host

    mounts = {m["Target"]: m for m in host["Mounts"]}
    assert set(mounts) == {"/in", "/ref/atlas", "/out", "/tmp"}
    assert mounts["/in"]["ReadOnly"] and mounts["/ref/atlas"]["ReadOnly"]
    assert not mounts["/out"]["ReadOnly"] and not mounts["/tmp"]["ReadOnly"]
    assert all(m["Type"] == "bind" for m in mounts.values())

    env = dict(item.split("=", 1) for item in body["Env"])
    assert env["HOME"] == "/tmp" and env["TMPDIR"] == "/tmp" and env["TZ"] == "UTC"
    assert env["STEP_CPUS"] == env["OMP_NUM_THREADS"] == "2"
    assert env["STEP_MEMORY_MB"] == "512" and env["STEP_GPUS"] == "0"

    work = Path(handle.data["work_dir"])
    assert json.loads((work / "in/params.json").read_text()) == {"seed": 7}
    assert (work / "in/lesion_mask.nii.gz").read_bytes() == b"x"
    assert handle.external_job_id == CONTAINER_ID
    assert handle.data["image_id"] == IMAGE_ID
    assert handle.data["timeout_seconds"] == 90
    json.dumps(handle.to_dict())  # persistable


def test_host_path_map_translates_bind_sources(tmp_path, docker):
    executor = make_executor(
        docker, host_path_map={tmp_path.resolve().as_posix(): "/srv/data"}
    )
    executor.submit(invocation(tmp_path))
    sources = {
        m["Target"]: m["Source"]
        for m in docker.bodies["create"]["HostConfig"]["Mounts"]
    }
    assert sources["/in"] == "/srv/data/attempt-1/in"
    assert sources["/out"] == "/srv/data/attempt-1/out"


def test_host_path_map_prefers_the_longest_prefix(tmp_path, docker):
    executor = make_executor(
        docker,
        host_path_map={
            tmp_path.resolve().as_posix(): "/a",
            (tmp_path / "attempt-1").resolve().as_posix(): "/b",
        },
    )
    assert executor.host_path(tmp_path / "attempt-1" / "in") == "/b/in"
    assert executor.host_path(tmp_path / "attempt-10") == "/a/attempt-10"


def test_parse_host_path_map():
    assert parse_host_path_map("/app/data=/srv/d, /x=/y") == {
        "/app/data": "/srv/d",
        "/x": "/y",
    }
    assert parse_host_path_map("") == {}
    with pytest.raises(ValueError):
        parse_host_path_map("nonsense")


def test_from_env(docker):
    executor = DockerExecutor.from_env(
        [IMAGE],
        {
            "DOCKER_PROXY_URL": "http://p:2375",
            "DOCKER_EXECUTOR_HOST_PATH_MAP": "/app/data=/srv/data",
            "DOCKER_EXECUTOR_USER": "1234:1234",
            "DOCKER_EXECUTOR_PIDS_LIMIT": "99",
            "DOCKER_EXECUTOR_PULL_MISSING": "true",
        },
        transport=docker.transport(),
    )
    assert executor.host_path_map == {"/app/data": "/srv/data"}
    assert executor.user == "1234:1234" and executor.pids_limit == 99
    assert executor.pull_missing is True and executor.allow_gpus is False


def test_failed_start_removes_container_and_staging(tmp_path, docker):
    original = docker.handle

    def failing(request):
        if request.url.path.endswith("/start"):
            return httpx.Response(500, json={"message": "no space"})
        return original(request)

    executor = make_executor(docker, transport=httpx.MockTransport(failing))
    with pytest.raises(ExecutorError, match="no space"):
        executor.submit(invocation(tmp_path))
    assert ("DELETE", f"/containers/{CONTAINER_ID}") in docker.calls
    if os.name == "posix":  # read-only staged files cannot be removed on Windows
        assert not list((tmp_path / "attempt-1").iterdir())


# --------------------------------------------------- poll, collect, persistence


def test_end_to_end_success_with_restart(tmp_path, docker):
    handle = make_executor(docker).submit(invocation(tmp_path))
    work = Path(handle.data["work_dir"])
    assert make_executor(docker).poll(handle).state is JobState.RUNNING

    (work / "out/lesion_qc.json").write_text("{}")
    (work / "out/provenance.json").write_text(json.dumps({"kind": "StepProvenance"}))
    (work / "out/extra").mkdir()
    docker.finish(0)

    # A new executor object and a handle restored from JSON: the worker restarted.
    restored = ExecutorHandle.from_dict(json.loads(json.dumps(handle.to_dict())))
    executor = make_executor(docker)
    status = executor.poll(restored)
    assert status.finished and status.outcome is Outcome.SUCCEEDED
    assert status.exit_code == 0
    assert status.started_at == "2026-11-13T10:00:00.123Z"
    assert status.finished_at == "2026-11-13T10:00:07.623Z"
    assert CONTAINER_ID not in docker.containers  # removed once the state was saved

    result = executor.collect(restored)
    assert result.succeeded and sorted(result.outputs) == ["lesion_qc.json"]
    assert result.provenance == {"kind": "StepProvenance"}
    assert result.extra_dir == work / "out/extra"
    assert result.usage.wall_seconds == 7.5
    assert result.log_path.read_text() == "INFO hello\nWARNING careful\n"
    assert result.execution["image"] == IMAGE
    assert result.execution["external_job_id"] == CONTAINER_ID
    assert result.execution["runtime"] == "docker"
    assert not list(execution_validator().iter_errors(result.execution))

    # idempotent, and works once the container is gone
    assert executor.collect(restored).execution == result.execution
    executor.cleanup(restored)
    executor.cleanup(restored)
    assert not (work / "out").exists()
    if os.name == "posix":
        assert not (work / "in").exists()
    assert result.log_path.exists()  # logs are kept


def test_rejected_and_failed_exit_codes(tmp_path, docker):
    executor = make_executor(docker)
    handle = executor.submit(invocation(tmp_path))
    docker.finish(10)
    assert executor.poll(handle).outcome is Outcome.REJECTED

    docker2 = FakeDocker()
    executor2 = make_executor(docker2)
    handle2 = executor2.submit(invocation(tmp_path / "second"))
    docker2.finish(1)
    assert executor2.poll(handle2).outcome is Outcome.FAILED


def test_out_of_memory_is_terminated_not_failed_exit(tmp_path, docker):
    executor = make_executor(docker)
    handle = executor.submit(invocation(tmp_path))
    docker.finish(137, oom=True)
    status = executor.poll(handle)
    assert status.outcome is Outcome.TERMINATED and status.exit_code is None
    result = executor.collect(handle)
    assert result.signal == 9 and "memory" in result.message
    assert result.execution["outcome"] == "failed"
    assert result.execution["exit_code"] is None
    assert not list(execution_validator().iter_errors(result.execution))


def test_signal_exit_codes_are_terminated(tmp_path, docker):
    executor = make_executor(docker)
    handle = executor.submit(invocation(tmp_path))
    docker.finish(139)
    status = executor.poll(handle)
    assert status.outcome is Outcome.TERMINATED
    assert executor.collect(handle).signal == 11


def test_queued_state(tmp_path, docker):
    executor = make_executor(docker)
    handle = executor.submit(invocation(tmp_path))
    docker.containers[CONTAINER_ID]["State"] = {"Status": "created"}
    assert executor.poll(handle).state is JobState.QUEUED


def test_collect_while_running_raises(tmp_path, docker):
    executor = make_executor(docker)
    handle = executor.submit(invocation(tmp_path))
    with pytest.raises(ExecutorError, match="still running"):
        executor.collect(handle)
    with pytest.raises(ExecutorError, match="running"):
        executor.cleanup(handle)


def test_lost_container_is_terminated(tmp_path, docker):
    executor = make_executor(docker)
    handle = executor.submit(invocation(tmp_path))
    docker.containers.clear()
    status = executor.poll(handle)
    assert status.outcome is Outcome.TERMINATED
    assert "lost track" in status.message


def test_foreign_handle_is_rejected(tmp_path, docker):
    handle = ExecutorHandle(executor="slurm", external_job_id="1", submitted_at="x")
    with pytest.raises(ExecutorError, match="belongs to"):
        make_executor(docker).poll(handle)


# ------------------------------------------------------------- cancel, timeout


def test_cancel_sends_sigterm_then_poll_escalates_to_sigkill(
    tmp_path, docker, monkeypatch
):
    clock = [1000.0]
    monkeypatch.setattr("orchestrator.executors.docker.time.time", lambda: clock[0])
    executor = make_executor(docker)
    handle = executor.submit(invocation(tmp_path))

    assert executor.cancel(handle) is True
    assert docker.signals == ["SIGTERM"]
    assert executor.cancel(handle) is True  # idempotent: no second signal
    assert docker.signals == ["SIGTERM"]

    clock[0] += 10
    assert executor.poll(handle).state is JobState.RUNNING
    assert docker.signals == ["SIGTERM"]

    clock[0] += 25  # past the 30 s grace period; a restarted worker does the same
    assert make_executor(docker).poll(handle).state is JobState.RUNNING
    assert docker.signals == ["SIGTERM", "SIGKILL"]
    make_executor(docker).poll(handle)
    assert docker.signals == ["SIGTERM", "SIGKILL"]  # sent once

    docker.finish(137)
    work = Path(handle.data["work_dir"])
    (work / "out/stale.nii.gz").write_text("partial")
    status = executor.poll(handle)
    assert status.outcome is Outcome.CANCELED
    result = executor.collect(handle)
    assert result.outputs == {} and result.provenance is None  # /out discarded
    assert result.execution["outcome"] == "canceled"
    assert executor.cancel(handle) is False  # already finished


def test_step_that_exits_cleanly_after_sigterm_is_still_canceled(tmp_path, docker):
    executor = make_executor(docker)
    handle = executor.submit(invocation(tmp_path))
    executor.cancel(handle)
    docker.finish(0)
    assert executor.poll(handle).outcome is Outcome.CANCELED


def test_timeout_sends_sigterm_once_the_wall_time_is_exceeded(tmp_path, docker):
    executor = make_executor(docker)
    handle = executor.submit(invocation(tmp_path, timeout_seconds=60))
    # The fake reports a start long in the past.
    docker.containers[CONTAINER_ID]["State"]["StartedAt"] = (
        "2020-01-01T00:00:00.000000000Z"
    )
    assert executor.poll(handle).state is JobState.RUNNING
    assert docker.signals == ["SIGTERM"]

    docker.finish(143, started="2020-01-01T00:00:00.000000000Z")
    status = executor.poll(handle)
    assert status.outcome is Outcome.TIMED_OUT
    assert "time limit" in executor.collect(handle).message


def test_no_timeout_when_zero(tmp_path, docker):
    executor = make_executor(docker)
    handle = executor.submit(invocation(tmp_path, timeout_seconds=0))
    docker.containers[CONTAINER_ID]["State"]["StartedAt"] = (
        "2020-01-01T00:00:00.000000000Z"
    )
    executor.poll(handle)
    assert docker.signals == []


# ------------------------------------------------------------------------ logs


def test_demux_handles_split_frames():
    data = frame(1, "alpha\n") + frame(2, "beta\n")
    demux = _Demux()
    out = []
    for i in range(0, len(data), 3):
        out.extend(demux.feed(data[i : i + 3]))
    assert b"".join(out) == b"alpha\nbeta\n"


def test_log_sink_keeps_head_and_tail_beyond_the_limit(tmp_path):
    path = tmp_path / "step.log"
    sink = _LogSink(path, limit=100)
    for i in range(50):
        sink.write(f"line-{i:03d}\n".encode())  # 9 bytes each, 450 in total
    sink.close()
    text = path.read_text()
    assert text.startswith("line-000\n") and text.endswith("line-049\n")
    assert "bytes truncated" in text
    assert "line-025" not in text
    assert len(text) < 100 + 60


def test_log_sink_small_log_is_verbatim(tmp_path):
    path = tmp_path / "step.log"
    sink = _LogSink(path, limit=100)
    sink.write(b"short\n")
    sink.close()
    assert path.read_text() == "short\n"


def test_log_failure_does_not_lose_the_result(tmp_path, docker):
    original = docker.handle

    def no_logs(request):
        if request.url.path.endswith("/logs"):
            return httpx.Response(500, json={"message": "log driver error"})
        return original(request)

    executor = make_executor(docker, transport=httpx.MockTransport(no_logs))
    handle = executor.submit(invocation(tmp_path))
    docker.finish(0)
    result = executor.collect(handle)
    assert result.succeeded
    assert "log capture incomplete" in result.log_path.read_text()
