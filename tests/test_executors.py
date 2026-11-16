"""Executor interface (orchestrator.executors.base) and the subprocess executor."""

from __future__ import annotations

import json
import os
import signal
import sys
import textwrap
import time
from pathlib import Path

import jsonschema
import pytest

from orchestrator.executors import (
    Accounting,
    BundleMount,
    Executor,
    ExecutorError,
    ExecutorHandle,
    ExecutorRefused,
    JobState,
    Outcome,
    Resources,
    StepInvocation,
    SubmitAs,
    SubprocessExecutor,
    classify_exit,
)
from orchestrator.executors.subprocess import process_alive

REPO_ROOT = Path(__file__).resolve().parents[1]
posix_only = pytest.mark.skipif(
    os.name != "posix", reason="subprocess executor is POSIX only"
)
EXECUTION_SCHEMA = None


def execution_validator():
    global EXECUTION_SCHEMA
    if EXECUTION_SCHEMA is None:
        full = json.loads(
            (REPO_ROOT / "contracts/schemas/v1/provenance.schema.json").read_text(
                encoding="utf-8"
            )
        )
        EXECUTION_SCHEMA = {
            "$schema": full["$schema"],
            "$defs": full["$defs"],
            "$ref": "#/$defs/execution",
        }
    return jsonschema.Draft202012Validator(EXECUTION_SCHEMA)


def py(code: str, *args: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(code), *args]


def invocation(tmp_path: Path, command, **overrides) -> StepInvocation:
    values = dict(
        step_run_id="sr-1",
        step_name="test-step",
        step_version="1.0.0",
        resources=Resources(cpus=2, memory_mb=512, wall_time_minutes=5),
        accounting=Accounting(submitted_by="user:8d2e6b1a", billing_account="lab-test"),
        work_dir=tmp_path / "attempt-1",
        command=command,
    )
    values.update(overrides)
    return StepInvocation(**values)


@pytest.fixture()
def executor():
    return SubprocessExecutor(kill_grace_seconds=2)


def run_to_end(executor, inv, timeout=30):
    handle = executor.submit(inv)
    executor.wait(handle, poll_interval=0.05, timeout=timeout)
    return handle, executor.collect(handle)


def wait_dead(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.05)
    return not process_alive(pid)


# --------------------------------------------------------------- interface, types


def test_interface_declares_the_five_operations():
    assert Executor.__abstractmethods__ == {
        "submit",
        "poll",
        "cancel",
        "collect",
        "cleanup",
    }
    assert issubclass(SubprocessExecutor, Executor)
    assert not SubprocessExecutor.__abstractmethods__
    assert SubprocessExecutor.name == "subprocess"
    assert SubprocessExecutor.runtime == "none"
    assert SubprocessExecutor.restartable is True


@pytest.mark.parametrize(
    ("exit_code", "reason", "expected", "recorded"),
    [
        (0, None, Outcome.SUCCEEDED, "succeeded"),
        (10, None, Outcome.REJECTED, "rejected"),
        (1, None, Outcome.FAILED, "failed"),
        (143, None, Outcome.FAILED, "failed"),
        (None, None, Outcome.TERMINATED, "failed"),
        (143, "canceled", Outcome.CANCELED, "canceled"),
        (None, "canceled", Outcome.CANCELED, "canceled"),
        (None, "timed_out", Outcome.TIMED_OUT, "timed_out"),
        (0, "timed_out", Outcome.TIMED_OUT, "timed_out"),
    ],
)
def test_exit_classification(exit_code, reason, expected, recorded):
    outcome = classify_exit(exit_code, stop_reason=reason)
    assert outcome is expected
    assert outcome.provenance_outcome == recorded


@pytest.mark.parametrize("policy", [SubmitAs.PER_USER, SubmitAs.CHARGEBACK])
def test_unimplemented_submit_policies_raise(tmp_path, policy):
    inv = invocation(
        tmp_path,
        ["true"],
        accounting=Accounting(submitted_by="user:abc", submit_as=policy),
    )
    with pytest.raises(NotImplementedError, match=policy.value):
        SubprocessExecutor().submit(inv)
    assert not inv.work_dir.exists()


def test_accounting_rejects_identifying_values():
    with pytest.raises(ValueError):
        Accounting(submitted_by="someone@example.org")
    with pytest.raises(ValueError):
        Accounting(submitted_by="user:abc", billing_account="bad account")
    assert Accounting(submitted_by="service:platform").to_dict() == {
        "submitted_by": "service:platform",
        "submit_as": "service-account",
        "billing_account": None,
    }


def test_invocation_validation_and_round_trip(tmp_path):
    with pytest.raises(ValueError):
        invocation(tmp_path, None, image="docker.io/org/step:latest")
    with pytest.raises(ValueError):
        invocation(tmp_path, None, inputs={"../x": tmp_path / "x"})
    inv = invocation(
        tmp_path,
        None,
        image="docker.io/org/step@sha256:" + "a" * 64,
        inputs={"ct.nii.gz": tmp_path / "ct.nii.gz"},
        params={"threshold": 0.5},
        bundles=[
            BundleMount(
                name="tracts", host_path=tmp_path / "b", id="toy", version="1.0.0"
            )
        ],
    )
    clone = StepInvocation.from_dict(json.loads(json.dumps(inv.to_dict())))
    assert clone == inv
    assert inv.effective_timeout_seconds == 300


def test_handle_round_trips_through_json():
    handle = ExecutorHandle(
        "subprocess", "123", "2026-11-13T10:00:00.000Z", {"work_dir": "/w"}
    )
    assert ExecutorHandle.from_dict(json.loads(json.dumps(handle.to_dict()))) == handle


# ------------------------------------------------------------------- outcomes


SUCCESS_STEP = """
import json, sys
from pathlib import Path
out = Path(sys.argv[1])
print("INFO writing output")
print("WARNING on stderr", file=sys.stderr)
(out / "result.txt").write_text("ok")
(out / "extra").mkdir()
(out / "extra" / "debug.txt").write_text("d")
(out / "provenance.json").write_text(json.dumps({"kind": "StepProvenance", "status": "succeeded"}))
"""


@posix_only
def test_success(tmp_path, executor):
    handle, result = run_to_end(
        executor, invocation(tmp_path, py(SUCCESS_STEP, "{out_dir}"))
    )
    assert result.outcome is Outcome.SUCCEEDED and result.succeeded
    assert result.exit_code == 0
    assert set(result.outputs) == {"result.txt"}
    assert result.provenance == {"kind": "StepProvenance", "status": "succeeded"}
    assert result.error is None
    assert result.extra_dir is not None
    log = result.log_path.read_text()
    assert "INFO writing output" in log and "WARNING on stderr" in log
    execution = dict(result.execution)
    execution_validator().validate(execution)
    assert execution["executor"] == "subprocess"
    assert execution["runtime"] == "none"
    assert execution["outcome"] == "succeeded"
    assert execution["resources"] == {
        "cpus": 2,
        "memory_mb": 512,
        "gpus": 0,
        "wall_time_minutes": 5,
    }
    assert execution["usage"]["wall_seconds"] >= 0
    assert executor.poll(handle).finished
    assert executor.cancel(handle) is False


@posix_only
def test_rejection_with_error_json(tmp_path, executor):
    code = """
    import json, sys
    from pathlib import Path
    (Path(sys.argv[1]) / "error.json").write_text(json.dumps({"category": "input_rejected", "code": "empty_mask"}))
    sys.exit(10)
    """
    _, result = run_to_end(executor, invocation(tmp_path, py(code, "{out_dir}")))
    assert result.outcome is Outcome.REJECTED
    assert result.exit_code == 10
    assert result.error["code"] == "empty_mask"
    assert result.outputs == {}
    execution_validator().validate(dict(result.execution))


@posix_only
def test_internal_failure(tmp_path, executor):
    _, result = run_to_end(
        executor, invocation(tmp_path, py("import sys; sys.exit(3)"))
    )
    assert result.outcome is Outcome.FAILED
    assert result.exit_code == 3
    assert result.execution["outcome"] == "failed"


@posix_only
def test_command_that_cannot_start_fails(tmp_path, executor):
    _, result = run_to_end(
        executor, invocation(tmp_path, [str(tmp_path / "missing-binary")])
    )
    assert result.outcome is Outcome.FAILED
    assert result.exit_code == 127
    assert "could not be started" in result.message


@posix_only
def test_timeout_stops_the_step_and_discards_out(tmp_path, executor):
    code = """
    import sys, time
    from pathlib import Path
    (Path(sys.argv[1]) / "partial.txt").write_text("x")
    time.sleep(60)
    """
    started = time.monotonic()
    _, result = run_to_end(
        executor, invocation(tmp_path, py(code, "{out_dir}"), timeout_seconds=1)
    )
    assert time.monotonic() - started < 15
    assert result.outcome is Outcome.TIMED_OUT
    assert result.outputs == {}
    assert result.execution["outcome"] == "timed_out"
    execution_validator().validate(dict(result.execution))


@posix_only
def test_cancel_graceful_step(tmp_path, executor):
    code = """
    import signal, sys, time
    def stop(*_):
        print("INFO stopping", flush=True)
        sys.exit(143)
    signal.signal(signal.SIGTERM, stop)
    print("INFO ready", flush=True)
    time.sleep(60)
    """
    inv = invocation(tmp_path, py(code))
    handle = executor.submit(inv)
    log = inv.work_dir / "logs" / "step.log"
    deadline = time.monotonic() + 10
    while "ready" not in log.read_text() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert executor.poll(handle).state is JobState.RUNNING
    assert executor.cancel(handle) is True
    status = executor.wait(handle, poll_interval=0.05, timeout=10)
    assert status.outcome is Outcome.CANCELED
    result = executor.collect(handle)
    assert result.exit_code == 143
    assert "INFO stopping" in result.log_path.read_text()


@posix_only
def test_cancel_escalates_to_sigkill(tmp_path, executor):
    code = """
    import signal, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    print("ready", flush=True)
    time.sleep(60)
    """
    inv = invocation(tmp_path, py(code))
    handle = executor.submit(inv)
    log = inv.work_dir / "logs" / "step.log"
    deadline = time.monotonic() + 10
    while "ready" not in log.read_text() and time.monotonic() < deadline:
        time.sleep(0.05)
    started = time.monotonic()
    executor.cancel(handle)
    executor.wait(handle, poll_interval=0.05, timeout=15)
    elapsed = time.monotonic() - started
    result = executor.collect(handle)
    assert result.outcome is Outcome.CANCELED
    assert result.exit_code is None and result.signal == signal.SIGKILL
    assert 1.5 <= elapsed < 10  # grace period of 2 s in the fixture


# --------------------------------------------------------------- process group


GRANDCHILD = "sleep 300 & echo $! > {tmp_dir}/grandchild.pid; "


@posix_only
def test_cancel_kills_the_whole_process_group(tmp_path, executor):
    inv = invocation(tmp_path, ["/bin/sh", "-c", GRANDCHILD + "wait"])
    handle = executor.submit(inv)
    pid_file = inv.work_dir / "tmp" / "grandchild.pid"
    deadline = time.monotonic() + 10
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    grandchild = int(pid_file.read_text())
    assert process_alive(grandchild)
    executor.cancel(handle)
    executor.wait(handle, poll_interval=0.05, timeout=10)
    assert wait_dead(grandchild)


@posix_only
def test_leftover_processes_are_killed_after_the_step_exits(tmp_path, executor):
    inv = invocation(tmp_path, ["/bin/sh", "-c", GRANDCHILD + "exit 0"])
    _, result = run_to_end(executor, inv)
    assert result.outcome is Outcome.SUCCEEDED
    grandchild = int((inv.work_dir / "tmp" / "grandchild.pid").read_text())
    assert wait_dead(grandchild)


# ----------------------------------------------------------- environment, staging


DUMP_ENV = """
import json, os, sys
from pathlib import Path
(Path(sys.argv[1]) / "env.json").write_text(json.dumps(dict(os.environ)))
"""


@posix_only
def test_clean_contract_environment(tmp_path, executor, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db/x")
    monkeypatch.setenv("UNRELATED_HOST_VARIABLE", "1")
    inv = invocation(
        tmp_path,
        py(DUMP_ENV, "{out_dir}"),
        resources=Resources(cpus=3, memory_mb=2048, wall_time_minutes=5),
    )
    _, result = run_to_end(executor, inv)
    env = json.loads(result.outputs["env.json"].read_text())
    tmp_dir = str(inv.work_dir.resolve() / "tmp")
    expected = {
        "HOME": tmp_dir,
        "TMPDIR": tmp_dir,
        "TZ": "UTC",
        "STEP_CPUS": "3",
        "STEP_MEMORY_MB": "2048",
        "STEP_GPUS": "0",
        "OMP_NUM_THREADS": "3",
        "MKL_NUM_THREADS": "3",
        "OPENBLAS_NUM_THREADS": "3",
        "ITK_GLOBAL_DEFAULT_NUMBER_OF_THREADS": "3",
    }
    for key, value in expected.items():
        assert env[key] == value
    # Only PATH passes through; Python itself may add LC_CTYPE (PEP 538).
    assert set(env) - set(expected) <= {"PATH", "LC_CTYPE"}


@posix_only
def test_inherited_environment_drops_secrets(tmp_path, executor, monkeypatch):
    monkeypatch.setenv("SMTP_PASSWORD", "x")
    monkeypatch.setenv("AUTH_SECRET_KEY", "x")
    monkeypatch.setenv("DATABASE_URL", "x")
    monkeypatch.setenv("FSLDIR", "/opt/fsl")
    inv = invocation(
        tmp_path,
        py(DUMP_ENV, "{out_dir}"),
        inherit_env=True,
        env={"EXTRA": "1", "TZ": "x"},
    )
    _, result = run_to_end(executor, inv)
    env = json.loads(result.outputs["env.json"].read_text())
    assert env["FSLDIR"] == "/opt/fsl"
    assert env["EXTRA"] == "1"
    assert env["TZ"] == "UTC"  # contract variables win
    for secret in ("SMTP_PASSWORD", "AUTH_SECRET_KEY", "DATABASE_URL"):
        assert secret not in env


@posix_only
def test_staging_of_inputs_params_and_bundles(tmp_path, executor):
    source = tmp_path / "mask.nii.gz"
    source.write_bytes(b"mask")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "bundle.yaml").write_text("id: toy\n")
    code = """
    import json, os, sys
    from pathlib import Path
    in_dir, out_dir, params, ref = map(Path, sys.argv[1:5])
    report = {
        "inputs": sorted(os.listdir(in_dir)),
        "params": json.loads(params.read_text()),
        "bundle": (ref / "tracts" / "bundle.yaml").read_text(),
        "writable_input": os.access(in_dir / "lesion_mask.nii.gz", os.W_OK),
    }
    (out_dir / "report.json").write_text(json.dumps(report))
    """
    inv = invocation(
        tmp_path,
        py(code, "{in_dir}", "{out_dir}", "{params}", "{ref_dir}"),
        inputs={"lesion_mask.nii.gz": source},
        params={"threshold": 0.5},
        bundles=[BundleMount(name="tracts", host_path=bundle)],
    )
    _, result = run_to_end(executor, inv)
    report = json.loads(result.outputs["report.json"].read_text())
    assert report["inputs"] == ["lesion_mask.nii.gz", "params.json"]
    assert report["params"] == {"threshold": 0.5}
    assert report["bundle"] == "id: toy\n"
    if os.geteuid() != 0:
        assert report["writable_input"] is False


@posix_only
def test_refusals(tmp_path, executor):
    with pytest.raises(ExecutorRefused):
        executor.submit(
            invocation(
                tmp_path,
                ["true"],
                resources=Resources(cpus=1, memory_mb=1, wall_time_minutes=1, gpus=1),
            )
        )
    work = tmp_path / "used"
    work.mkdir()
    (work / "leftover").write_text("x")
    with pytest.raises(ExecutorError):
        executor.submit(invocation(tmp_path, ["true"], work_dir=work))


@posix_only
def test_cleanup_keeps_logs(tmp_path, executor):
    inv = invocation(tmp_path, py("import time; time.sleep(30)"))
    handle = executor.submit(inv)
    with pytest.raises(ExecutorError):
        executor.cleanup(handle)
    with pytest.raises(ExecutorError):
        executor.collect(handle)
    executor.cancel(handle)
    executor.wait(handle, poll_interval=0.05, timeout=10)
    executor.cleanup(handle)
    executor.cleanup(handle)  # idempotent
    for name in ("in", "out", "tmp", "ref"):
        assert not (inv.work_dir / name).exists()
    assert (inv.work_dir / "logs" / "step.log").exists()


# -------------------------------------------------------------------- restarts


@posix_only
def test_poll_and_collect_from_a_persisted_handle(tmp_path):
    first = SubprocessExecutor()
    inv = invocation(tmp_path, py("import sys, time; time.sleep(1); sys.exit(10)"))
    stored = json.dumps(first.submit(inv).to_dict())
    del first  # the process that submitted is gone

    second = SubprocessExecutor()
    handle = ExecutorHandle.from_dict(json.loads(stored))
    assert second.poll(handle).state is JobState.RUNNING
    status = second.wait(handle, poll_interval=0.05, timeout=15)
    assert status.outcome is Outcome.REJECTED
    assert second.collect(handle).exit_code == 10


@posix_only
def test_cancel_from_a_persisted_handle(tmp_path):
    inv = invocation(tmp_path, py("import time; time.sleep(60)"))
    stored = SubprocessExecutor().submit(inv).to_dict()
    other = SubprocessExecutor(kill_grace_seconds=2)
    handle = ExecutorHandle.from_dict(stored)
    assert other.cancel(handle) is True
    assert (
        other.wait(handle, poll_interval=0.05, timeout=15).outcome is Outcome.CANCELED
    )


@posix_only
def test_lost_supervisor_is_reported_and_its_group_killed(tmp_path):
    inv = invocation(tmp_path, ["/bin/sh", "-c", GRANDCHILD + "wait"])
    handle = SubprocessExecutor().submit(inv)
    pid_file = inv.work_dir / "tmp" / "grandchild.pid"
    deadline = time.monotonic() + 10
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline
        time.sleep(0.05)
    grandchild = int(pid_file.read_text())
    os.kill(int(handle.data["supervisor_pid"]), signal.SIGKILL)
    assert wait_dead(int(handle.data["supervisor_pid"]))

    status = SubprocessExecutor().poll(ExecutorHandle.from_dict(handle.to_dict()))
    assert status.finished
    assert status.outcome is Outcome.TERMINATED
    assert "lost track" in status.message
    assert wait_dead(grandchild)


# ---------------------------------------------------------------------- adopt


def test_adopt_is_optional_on_the_interface(tmp_path):
    class Minimal(Executor):
        name = "minimal"
        runtime = "none"

        def submit(self, invocation): ...
        def poll(self, handle): ...
        def cancel(self, handle): ...
        def collect(self, handle): ...
        def cleanup(self, handle): ...

    with pytest.raises(NotImplementedError, match="cannot adopt"):
        Minimal().adopt(invocation(tmp_path, ["true"]))


def _wait_for(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.05)


@posix_only
def test_adopt_finds_the_running_supervisor_of_a_lost_submission(tmp_path):
    inv = invocation(tmp_path, py("import time; time.sleep(60)"))
    lost = SubprocessExecutor().submit(inv)  # the handle is never recorded
    _wait_for(inv.work_dir / ".executor" / "supervisor.json")

    other = SubprocessExecutor(kill_grace_seconds=2)  # another worker process
    handle = other.adopt(inv)
    assert handle.data["supervisor_pid"] == lost.data["supervisor_pid"]
    assert handle.data["supervisor_start_ticks"] == lost.data["supervisor_start_ticks"]
    assert other.poll(handle).state is JobState.RUNNING
    assert other.cancel(handle) is True
    status = other.wait(handle, poll_interval=0.05, timeout=15)
    assert status.outcome is Outcome.CANCELED
    other.cleanup(handle)
    assert not (inv.work_dir / "in").exists()


@posix_only
def test_adopt_a_finished_attempt_and_collect_it(tmp_path):
    inv = invocation(
        tmp_path, py("import sys; open(sys.argv[1] + '/a.txt', 'w')", "{out_dir}")
    )
    first = SubprocessExecutor()
    first.wait(first.submit(inv), poll_interval=0.05, timeout=15)

    handle = SubprocessExecutor().adopt(inv)
    result = SubprocessExecutor().collect(handle)
    assert result.succeeded and sorted(result.outputs) == ["a.txt"]


def test_adopt_without_a_job_returns_none_or_refuses_an_unknown_state(tmp_path):
    inv = invocation(tmp_path, ["true"])
    state = inv.work_dir / ".executor"
    state.mkdir(parents=True)
    (inv.work_dir / "in").mkdir()
    (inv.work_dir / "in" / "params.json").write_text("{}")
    # Without a request record: submit stopped before it, nothing was started.
    assert SubprocessExecutor().adopt(inv) is None
    assert not (inv.work_dir / "in").exists()
    # With a request but no supervisor record the state is unknown (e.g. a
    # supervisor of an earlier release): refused, and nothing is deleted.
    (inv.work_dir / "out").mkdir()
    (state / "request.json").write_text(json.dumps(inv.to_dict()))
    (state / "child.json").write_text(json.dumps({"pid": 1, "pgid": 1}))
    with pytest.raises(ExecutorRefused, match="cannot be established"):
        SubprocessExecutor().adopt(inv)
    assert (inv.work_dir / "out").is_dir()
    assert (state / "request.json").exists()


@posix_only
def test_lost_group_is_signalled_only_when_its_leader_is_verified(monkeypatch):
    import subprocess as sp

    from orchestrator.executors import subprocess as module

    sent = []
    monkeypatch.setattr(module, "_killpg", lambda pgid, sig: sent.append(pgid))
    alive = sp.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        ticks = module._proc_start_ticks(alive.pid)
        # A live process under the recorded PID that is not the step: untouched.
        module._kill_lost_group({"pgid": alive.pid, "start_ticks": (ticks or 0) + 1})
        # No start time recorded (earlier release): cannot be verified, untouched.
        module._kill_lost_group({"pgid": alive.pid})
        assert sent == []
        # The recorded leader itself: signalled.
        module._kill_lost_group({"pgid": alive.pid, "start_ticks": ticks})
        assert sent == ([alive.pid] if ticks is not None else [])
    finally:
        alive.kill()
        alive.wait()
    # Leader gone: the group id cannot have been reused while members remain.
    module._kill_lost_group({"pgid": alive.pid, "start_ticks": ticks})
    assert sent[-1] == alive.pid


@posix_only
def test_child_record_carries_the_start_time_of_the_group_leader(tmp_path, executor):
    inv = invocation(tmp_path, py("import time; time.sleep(0.2)"))
    handle, _result = run_to_end(executor, inv)
    child = json.loads((inv.work_dir / ".executor" / "child.json").read_text())
    assert child["pgid"] == child["pid"]
    if Path("/proc/self/stat").exists():
        assert isinstance(child["start_ticks"], int)


def test_adopt_refuses_a_work_directory_of_another_attempt(tmp_path):
    inv = invocation(tmp_path, ["true"])
    state = inv.work_dir / ".executor"
    state.mkdir(parents=True)
    (inv.work_dir / "out").mkdir()
    other = invocation(tmp_path, ["true"], attempt=2)
    (state / "request.json").write_text(json.dumps(other.to_dict()))
    with pytest.raises(ExecutorRefused, match="another attempt"):
        SubprocessExecutor().adopt(inv)
    (state / "request.json").write_text("not json")
    with pytest.raises(ExecutorRefused, match="unreadable"):
        SubprocessExecutor().adopt(inv)
    assert (inv.work_dir / "out").is_dir()  # nothing touched


# ------------------------------------------------------------ legacy adapters


def _dummy_command(*extra: str) -> str:
    script = REPO_ROOT / "external_processor" / "dummy_processor.py"
    return (
        f"{sys.executable} {script} --input {{input_dir}} --output {{output_dir}} "
        f"--study-id {{study_id}} " + " ".join(extra)
    )


def _legacy(
    tmp_path, backend="dummy", processor_command=None, compneuro_command="true", **kw
):
    from processor_adapter.executor_bridge import legacy_invocation

    input_dir = tmp_path / "input"
    input_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / "study.nii").write_text("dummy", encoding="utf-8")
    return legacy_invocation(
        backend=backend,
        processor_command=processor_command or _dummy_command("--sleep", "0"),
        compneuro_command=compneuro_command,
        compneuro_project_mount=tmp_path / "project",
        input_dir=input_dir,
        output_dir=tmp_path / "output",
        study_id="abc",
        logs_dir=tmp_path / "logs",
        work_dir=tmp_path / "attempt-1",
        step_run_id="legacy-1",
        **kw,
    )


@posix_only
def test_dummy_adapter_through_the_interface(tmp_path):
    from processor_adapter.executor_bridge import run_legacy

    result = run_legacy(SubprocessExecutor(), _legacy(tmp_path), poll_interval=0.05)
    assert result.success is True
    assert result.exit_code == 0
    assert result.pdf_path is not None and result.pdf_path.exists()
    assert result.pdf_path.parent == tmp_path / "output"
    assert result.log_path.exists()
    assert not (tmp_path / "attempt-1" / "out").exists()  # cleaned up


@posix_only
def test_dummy_adapter_failure_through_the_interface(tmp_path):
    from processor_adapter.executor_bridge import run_legacy

    inv = _legacy(tmp_path, processor_command=_dummy_command("--sleep", "0", "--fail"))
    result = run_legacy(SubprocessExecutor(), inv, poll_interval=0.05)
    assert result.success is False
    assert result.exit_code != 0
    assert result.error_message == "The external processor finished with an error"


@posix_only
def test_dummy_adapter_cancel_and_timeout_through_the_interface(tmp_path):
    from processor_adapter.cancellation import ProcessorCanceled
    from processor_adapter.executor_bridge import result_from_step, run_legacy

    executor = SubprocessExecutor(kill_grace_seconds=5)
    inv = _legacy(tmp_path, processor_command=_dummy_command("--sleep", "60"))
    handle = executor.submit(inv)
    time.sleep(1)
    executor.cancel(handle)
    executor.wait(handle, poll_interval=0.05, timeout=20)
    with pytest.raises(ProcessorCanceled):
        result_from_step(executor.collect(handle), dict(inv.params))

    inv = _legacy(
        tmp_path / "t",
        processor_command=_dummy_command("--sleep", "60"),
        timeout_seconds=1,
    )
    result = run_legacy(executor, inv, poll_interval=0.05)
    assert result.success is False
    assert result.error_message == "The external processor exceeded the time limit"


@posix_only
def test_compneuro_adapter_through_the_interface(tmp_path):
    from processor_adapter.executor_bridge import run_legacy

    bids = tmp_path / "bids_project"
    anat = bids / "data" / "sub-O01" / "anat"
    anat.mkdir(parents=True)
    (anat / "sub-O01_T1w.nii.gz").write_bytes(b"nifti")
    inv = _legacy(
        tmp_path,
        backend="compneuro",
        compneuro_command=(
            "mkdir -p {project_dir}/Preproc/BET {project_dir}/Preproc/ProbTissue && "
            "touch {project_dir}/Preproc/BET/sub-O01_T1w_brain.nii.gz "
            "{project_dir}/Preproc/ProbTissue/sub-O01_T1w_brain_CSF.nii.gz"
        ),
        bids_project_dir=bids,
        runtime_project_dir=tmp_path / "runtime_project",
    )
    result = run_legacy(SubprocessExecutor(), inv, poll_interval=0.05)
    assert result.success is True, result.error_message
    assert result.preproc_path == tmp_path / "output" / "Preproc"
    assert len(result.output_files) == 2
