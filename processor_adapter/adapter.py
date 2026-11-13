from __future__ import annotations

import fcntl
import os
import signal
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from processor_adapter.artifacts import list_output_files
from processor_adapter.cancellation import ProcessorCanceled


@dataclass(frozen=True)
class ProcessorResult:
    success: bool
    exit_code: int | None
    pdf_path: Path | None
    log_path: Path
    error_message: str | None
    duration_seconds: float
    output_files: list[Path] = field(default_factory=list)
    output_zip_path: Path | None = None
    preproc_path: Path | None = None
    warnings: list[str] = field(default_factory=list)


class DummyProcessorAdapter:
    """Adapter for the development processor that generates a PDF."""

    def __init__(self, command_template: str, timeout_seconds: int = 0) -> None:
        self.command_template = command_template
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        input_dir: Path,
        output_dir: Path,
        study_id: str,
        logs_dir: Path,
        **_: object,
    ) -> ProcessorResult:
        started = time.monotonic()
        input_dir = input_dir.resolve()
        output_dir = output_dir.resolve()
        logs_dir = logs_dir.resolve()
        log_path = logs_dir / "processor.log"

        if not input_dir.exists() or not input_dir.is_dir():
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path.write_text("Input directory does not exist\n", encoding="utf-8")
            return ProcessorResult(False, None, None, log_path, "Input directory does not exist", 0.0)

        output_dir.mkdir(parents=True, exist_ok=True)
        logs_dir.mkdir(parents=True, exist_ok=True)

        command = self.command_template.format(
            input_dir=shlex.quote(str(input_dir)),
            output_dir=shlex.quote(str(output_dir)),
            study_id=shlex.quote(study_id),
            logs_dir=shlex.quote(str(logs_dir)),
        )

        completed, duration = self._run_command(command, log_path, started)
        if completed is None:
            return ProcessorResult(False, None, None, log_path, "Error running the external processor", duration)
        if completed.returncode != 0:
            return ProcessorResult(False, completed.returncode, None, log_path, "The external processor finished with an error", duration)

        pdf_path = self._find_pdf(output_dir)
        if not pdf_path:
            return ProcessorResult(False, completed.returncode, None, log_path, "The processor did not generate any PDF", duration)

        return ProcessorResult(True, completed.returncode, pdf_path, log_path, None, duration, output_files=[pdf_path])

    def _run_command(self, command: str, log_path: Path, started: float) -> tuple[subprocess.CompletedProcess[str] | None, float]:
        try:
            completed = _run_external_command(command, self.timeout_seconds)
        except ProcessorCanceled:
            duration = time.monotonic() - started
            log_path.write_text(f"COMMAND: {command}\nCANCELED: true\n", encoding="utf-8")
            raise
        except Exception as exc:  # pragma: no cover - defensive around external tools
            duration = time.monotonic() - started
            log_path.write_text(f"COMMAND: {command}\nERROR: {exc}\n", encoding="utf-8")
            return None, duration

        duration = time.monotonic() - started
        log_path.write_text(
            "\n".join(
                [
                    f"COMMAND: {command}",
                    f"EXIT_CODE: {completed.returncode}",
                    "STDOUT:",
                    completed.stdout,
                    "STDERR:",
                    completed.stderr,
                ]
            ),
            encoding="utf-8",
        )
        return completed, duration

    def _find_pdf(self, output_dir: Path) -> Path | None:
        pdfs = sorted(output_dir.rglob("*.pdf"))
        return pdfs[0] if pdfs else None


class CompneuroAnatprocAdapter:
    """Adapter for compneuro-anatproc, which writes Preproc outputs instead of PDFs."""

    def __init__(self, command_template: str, project_mount: Path, timeout_seconds: int = 0) -> None:
        self.command_template = command_template
        self.project_mount = project_mount
        self.timeout_seconds = timeout_seconds

    def run(
        self,
        input_dir: Path,
        output_dir: Path,
        study_id: str,
        logs_dir: Path,
        *,
        bids_project_dir: Path | None = None,
        runtime_project_dir: Path | None = None,
        **_: object,
    ) -> ProcessorResult:
        started = time.monotonic()
        output_dir = output_dir.resolve()
        logs_dir = logs_dir.resolve()
        log_path = logs_dir / "processor.log"
        preproc_dir = output_dir / "Preproc"
        logs_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        bids_project_dir = (bids_project_dir or input_dir).resolve()
        runtime_project_dir = (runtime_project_dir or output_dir.parent / "runtime_project").resolve()

        with Path("/tmp/compneuro-project.lock").open("w", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                try:
                    self._prepare_project_mount(bids_project_dir, runtime_project_dir, preproc_dir)
                except Exception as exc:
                    duration = time.monotonic() - started
                    log_path.write_text(f"ERROR preparando /project: {exc}\n", encoding="utf-8")
                    return ProcessorResult(False, None, None, log_path, "Could not prepare the /project environment", duration)

                command = self.command_template.format(
                    input_dir=shlex.quote(str(input_dir.resolve())),
                    output_dir=shlex.quote(str(output_dir)),
                    study_id=shlex.quote(study_id),
                    logs_dir=shlex.quote(str(logs_dir)),
                    project_dir=shlex.quote(str(runtime_project_dir)),
                )

                completed, duration = self._run_command(command, log_path, started)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
        if completed is None:
            return ProcessorResult(False, None, None, log_path, "Error ejecutando compneuro-anatproc", duration, preproc_path=preproc_dir)
        if completed.returncode != 0:
            return ProcessorResult(False, completed.returncode, None, log_path, "compneuro-anatproc finished with an error", duration, preproc_path=preproc_dir)

        output_files = list_output_files(preproc_dir)
        missing = [name for name in ["BET", "ProbTissue"] if not (preproc_dir / name).is_dir()]
        if missing:
            return ProcessorResult(
                False,
                completed.returncode,
                None,
                log_path,
                f"The expected folders were not generated: {', '.join(missing)}",
                duration,
                output_files=output_files,
                preproc_path=preproc_dir,
            )
        if not output_files:
            return ProcessorResult(False, completed.returncode, None, log_path, "compneuro-anatproc did not generate outputs", duration, preproc_path=preproc_dir)

        return ProcessorResult(True, completed.returncode, None, log_path, None, duration, output_files=output_files, preproc_path=preproc_dir)

    def _prepare_project_mount(self, bids_project_dir: Path, runtime_project_dir: Path, preproc_dir: Path) -> None:
        bids_data_dir = bids_project_dir / "data"
        if not bids_data_dir.is_dir():
            raise ValueError("No existe bids_project/data")

        runtime_project_dir.mkdir(parents=True, exist_ok=True)
        preproc_dir.mkdir(parents=True, exist_ok=True)
        self._replace_symlink(runtime_project_dir / "data", bids_data_dir)
        self._replace_symlink(runtime_project_dir / "Preproc", preproc_dir)

        project_mount = self.project_mount
        if project_mount.resolve() == runtime_project_dir:
            return
        if project_mount.is_symlink():
            project_mount.unlink()
        elif project_mount.exists():
            raise ValueError(f"{project_mount} exists and is not a managed symlink")
        project_mount.symlink_to(runtime_project_dir, target_is_directory=True)

    def _replace_symlink(self, link_path: Path, target: Path) -> None:
        if link_path.is_symlink():
            link_path.unlink()
        elif link_path.exists():
            raise ValueError(f"{link_path} exists and is not a managed symlink")
        link_path.symlink_to(target, target_is_directory=True)

    def _run_command(self, command: str, log_path: Path, started: float) -> tuple[subprocess.CompletedProcess[str] | None, float]:
        env = os.environ.copy()
        env.setdefault("PROJECT_PATH", str(self.project_mount))
        try:
            completed = _run_external_command(command, self.timeout_seconds, env=env)
        except ProcessorCanceled:
            duration = time.monotonic() - started
            log_path.write_text(
                f"COMMAND: {command}\nPROJECT_MOUNT: {self.project_mount}\nCANCELED: true\n",
                encoding="utf-8",
            )
            raise
        except Exception as exc:  # pragma: no cover - defensive around external tools
            duration = time.monotonic() - started
            log_path.write_text(f"COMMAND: {command}\nERROR: {exc}\n", encoding="utf-8")
            return None, duration

        duration = time.monotonic() - started
        log_path.write_text(
            "\n".join(
                [
                    f"COMMAND: {command}",
                    f"PROJECT_MOUNT: {self.project_mount}",
                    f"EXIT_CODE: {completed.returncode}",
                    "STDOUT:",
                    completed.stdout,
                    "STDERR:",
                    completed.stderr,
                ]
            ),
            encoding="utf-8",
        )
        return completed, duration


ProcessorAdapter = DummyProcessorAdapter


def _run_external_command(
    command: str,
    timeout_seconds: int = 0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    previous_handlers = {
        signal.SIGTERM: signal.getsignal(signal.SIGTERM),
        signal.SIGINT: signal.getsignal(signal.SIGINT),
    }

    def handle_cancel(signum, _frame):
        _terminate_process_group(process)
        raise ProcessorCanceled(f"Processing canceled by signal {signum}")

    for signum in previous_handlers:
        signal.signal(signum, handle_cancel)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds or None)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        raise
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    process_group_id = process.pid
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is None:
            try:
                process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)

    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def create_processor_adapter(
    *,
    backend: str,
    processor_command: str,
    compneuro_command: str,
    compneuro_project_mount: Path,
    timeout_seconds: int = 0,
) -> DummyProcessorAdapter | CompneuroAnatprocAdapter:
    normalized_backend = backend.strip().lower()
    if normalized_backend == "dummy":
        return DummyProcessorAdapter(processor_command, timeout_seconds=timeout_seconds)
    if normalized_backend == "compneuro":
        return CompneuroAnatprocAdapter(compneuro_command, compneuro_project_mount, timeout_seconds=timeout_seconds)
    raise ValueError(f"Unsupported processing backend: {backend}")
