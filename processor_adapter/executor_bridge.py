"""Run the legacy Dummy and CompneuroAnatproc adapters through the executor interface.

The adapters themselves are unchanged. ``legacy_invocation`` builds a
``StepInvocation`` whose command is this module run as a program::

    python -m processor_adapter.executor_bridge --params <in>/params.json --out <out>

Inside the executor's process group, the program builds the configured adapter
with ``create_processor_adapter`` and calls ``adapter.run`` exactly as the worker
does today (global lock and ``/project`` symlink for anatproc included), then
writes the ``ProcessorResult`` to ``<out>/extra/legacy_result.json`` and exits
0 on success, 1 on failure, 143 when canceled. ``result_from_step`` turns the
collected ``StepResult`` back into a ``ProcessorResult`` so that the worker's
post-processing can stay as it is when F0-15 switches it to the orchestrator.

Timeouts and cancellation are owned by the executor: the adapter runs with
``timeout_seconds=0``; on SIGTERM its own handler stops the external command's
process group (which it starts in a new session) and the program exits.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from processor_adapter.adapter import ProcessorResult, create_processor_adapter
from processor_adapter.cancellation import ProcessorCanceled

RESULT_FILE = "legacy_result.json"
EXIT_TERMINATED = 143
LEGACY_STEP_NAME = {"dummy": "legacy-dummy", "compneuro": "legacy-compneuro-anatproc"}
_REPO_ROOT = Path(__file__).resolve().parents[1]


def legacy_invocation(
    *,
    backend: str,
    processor_command: str,
    compneuro_command: str,
    compneuro_project_mount: Path,
    input_dir: Path,
    output_dir: Path,
    study_id: str,
    logs_dir: Path,
    work_dir: Path,
    step_run_id: str,
    bids_project_dir: Path | None = None,
    runtime_project_dir: Path | None = None,
    timeout_seconds: int = 0,
    accounting=None,
    resources=None,
    processor_version: str = "0.0.0",
):
    """A ``StepInvocation`` running one legacy adapter (``backend``: dummy or compneuro)."""
    from orchestrator.executors.base import Accounting, Resources, StepInvocation

    normalized = backend.strip().lower()
    if normalized not in LEGACY_STEP_NAME:
        raise ValueError(f"Unsupported processing backend: {backend}")
    params = {
        "backend": normalized,
        "processor_command": processor_command,
        "compneuro_command": compneuro_command,
        "compneuro_project_mount": str(compneuro_project_mount),
        "input_dir": str(input_dir),
        "output_dir": str(output_dir),
        "study_id": study_id,
        "logs_dir": str(logs_dir),
        "bids_project_dir": str(bids_project_dir) if bids_project_dir else None,
        "runtime_project_dir": str(runtime_project_dir)
        if runtime_project_dir
        else None,
        # Legacy commands may use relative paths: keep the worker's directory.
        "cwd": os.getcwd(),
    }
    pythonpath = os.pathsep.join(
        p for p in [str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")] if p
    )
    return StepInvocation(
        step_run_id=step_run_id,
        step_name=LEGACY_STEP_NAME[normalized],
        step_version=processor_version,
        resources=resources
        or Resources(
            cpus=os.cpu_count() or 1, memory_mb=4096, wall_time_minutes=24 * 60
        ),
        accounting=accounting or Accounting(submitted_by="service:platform"),
        work_dir=work_dir,
        params=params,
        timeout_seconds=timeout_seconds,
        command=[
            sys.executable,
            "-m",
            "processor_adapter.executor_bridge",
            "--params",
            "{params}",
            "--out",
            "{out_dir}",
        ],
        env={"PYTHONPATH": pythonpath},
        inherit_env=True,
    )


def _result_to_dict(result: ProcessorResult) -> dict[str, Any]:
    def path(value):
        return str(value) if value is not None else None

    return {
        "success": result.success,
        "exit_code": result.exit_code,
        "pdf_path": path(result.pdf_path),
        "log_path": str(result.log_path),
        "error_message": result.error_message,
        "duration_seconds": result.duration_seconds,
        "output_files": [str(p) for p in result.output_files],
        "output_zip_path": path(result.output_zip_path),
        "preproc_path": path(result.preproc_path),
        "warnings": list(result.warnings),
    }


def _result_from_dict(data: dict[str, Any]) -> ProcessorResult:
    def path(value):
        return Path(value) if value else None

    return ProcessorResult(
        success=bool(data["success"]),
        exit_code=data.get("exit_code"),
        pdf_path=path(data.get("pdf_path")),
        log_path=Path(data["log_path"]),
        error_message=data.get("error_message"),
        duration_seconds=float(data.get("duration_seconds") or 0.0),
        output_files=[Path(p) for p in data.get("output_files", [])],
        output_zip_path=path(data.get("output_zip_path")),
        preproc_path=path(data.get("preproc_path")),
        warnings=list(data.get("warnings", [])),
    )


def result_from_step(step_result, params: dict[str, Any]) -> ProcessorResult:
    """Convert a collected ``StepResult`` of a legacy invocation to ``ProcessorResult``.

    Raises ``ProcessorCanceled`` for a canceled attempt, as ``adapter.run`` does.
    """
    from orchestrator.executors.base import Outcome

    if step_result.outcome is Outcome.CANCELED:
        raise ProcessorCanceled("Processing canceled")
    log_path = Path(params["logs_dir"]) / "processor.log"
    output_dir = Path(params["output_dir"])
    preproc = output_dir / "Preproc" if params["backend"] == "compneuro" else None
    duration = step_result.usage.wall_seconds or 0.0
    if step_result.outcome is Outcome.TIMED_OUT:
        return ProcessorResult(
            False,
            None,
            None,
            log_path,
            "The external processor exceeded the time limit",
            duration,
            preproc_path=preproc,
        )
    result_file = step_result.extra_dir / RESULT_FILE if step_result.extra_dir else None
    if result_file is None or not result_file.is_file():
        return ProcessorResult(
            False,
            step_result.exit_code,
            None,
            log_path,
            "Error running the external processor",
            duration,
            preproc_path=preproc,
        )
    return _result_from_dict(json.loads(result_file.read_text(encoding="utf-8")))


def run_legacy(executor, invocation, *, poll_interval: float = 1.0) -> ProcessorResult:
    """Submit, wait, collect and clean up one legacy attempt; returns ``ProcessorResult``."""
    handle = executor.submit(invocation)
    try:
        executor.wait(handle, poll_interval=poll_interval)
        step_result = executor.collect(handle)
        return result_from_step(step_result, dict(invocation.params))
    finally:
        if executor.poll(handle).finished:
            executor.cleanup(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a legacy processor adapter once.")
    parser.add_argument("--params", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    params = json.loads(Path(args.params).read_text(encoding="utf-8"))
    if params.get("cwd"):
        os.chdir(params["cwd"])

    adapter = create_processor_adapter(
        backend=params["backend"],
        processor_command=params["processor_command"],
        compneuro_command=params["compneuro_command"],
        compneuro_project_mount=Path(params["compneuro_project_mount"]),
        timeout_seconds=0,
    )
    optional = {
        key: Path(params[key])
        for key in ("bids_project_dir", "runtime_project_dir")
        if params.get(key)
    }
    try:
        result = adapter.run(
            input_dir=Path(params["input_dir"]),
            output_dir=Path(params["output_dir"]),
            study_id=params["study_id"],
            logs_dir=Path(params["logs_dir"]),
            **optional,
        )
    except ProcessorCanceled:
        print("INFO legacy adapter canceled", flush=True)
        return EXIT_TERMINATED

    extra = Path(args.out) / "extra"
    extra.mkdir(parents=True, exist_ok=True)
    (extra / RESULT_FILE).write_text(
        json.dumps(_result_to_dict(result), indent=2), encoding="utf-8"
    )
    print(
        f"INFO legacy adapter finished: success={result.success} exit_code={result.exit_code}",
        flush=True,
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
