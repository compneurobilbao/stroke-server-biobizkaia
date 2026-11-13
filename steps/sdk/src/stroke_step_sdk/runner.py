"""The step lifecycle (contract sections 2 to 5)."""

from __future__ import annotations

import logging
import os
import platform
import signal
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

from . import __version__, _env, contracts, formats, logging_setup, provenance
from . import params as params_mod
from .context import StepContext
from .errors import (
    InputRejected,
    InternalFailure,
    ManifestError,
    StepRejection,
    Terminated,
)
from .hashing import file_record
from .manifest import DEFAULT_MANIFEST_PATH, Manifest, load_manifest

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_REJECTED = 10
EXIT_TERMINATED = 143  # 128 + SIGTERM

PROVENANCE_FILE = "provenance.json"
ERROR_FILE = "error.json"
EXTRA_DIR = "extra"

RunFunction = Callable[[StepContext], None]


class _Run:
    def __init__(
        self,
        run_fn: RunFunction,
        manifest: Manifest,
        in_dir: Path,
        out_dir: Path,
        params_path: Path,
        ref_dir: Path,
        environ: Mapping[str, str],
        logger: logging.Logger,
    ) -> None:
        self.run_fn = run_fn
        self.manifest = manifest
        self.in_dir = in_dir
        self.out_dir = out_dir
        self.params_path = params_path
        self.ref_dir = ref_dir
        self.environ = environ
        self.log = logger
        self.started_at = provenance.utc_now()
        self.params: dict[str, Any] = {}
        self.input_records: list[dict] = []
        self.bundle_records: list[dict] = []
        self.ctx: StepContext | None = None

    # --- driver -------------------------------------------------------------

    def execute(self) -> int:
        m = self.manifest
        self.log.info(
            "starting step %s %s (contract %s)", m.name, m.version, m.contract_version
        )
        for code in m.missing_sdk_codes():
            self.log.warning(
                "the manifest does not declare the standard error code '%s'", code
            )
        try:
            self._prepare()
            assert self.ctx is not None
            self.run_fn(self.ctx)
            return self._succeed()
        except StepRejection as exc:
            return self._reject(exc)
        except Terminated as exc:
            self.log.error("received signal %d, stopping", exc.signum)
            return self._internal(
                "terminated",
                "The step was terminated before it finished.",
                exit_code=EXIT_TERMINATED,
            )
        except InternalFailure as exc:
            self.log.error("%s: %s", exc.code, exc.message)
            return self._internal(exc.code, exc.message, exc.details)
        except Exception as exc:
            self.log.error("unhandled %s", type(exc).__name__, exc_info=True)
            return self._internal(
                "internal_error",
                "The step failed with an internal error.",
                {"exception": type(exc).__name__},
            )
        except SystemExit as exc:
            self.log.error("the step called exit(%s)", exc.code)
            return self._internal(
                "unexpected_exit", "The step exited without reporting a result."
            )

    # --- preparation ------------------------------------------------------------

    def _prepare(self) -> None:
        m = self.manifest
        try:
            supplied = params_mod.read_params_file(self.params_path)
        except params_mod.ParameterError as exc:
            raise InternalFailure("params_unreadable", f"Parameters: {exc}.") from None
        self.params = dict(supplied)
        try:
            self.params = params_mod.resolve(m.parameters_schema, supplied)
        except params_mod.ParameterError as exc:
            self.log.error("invalid parameters: %s", exc)
            raise InputRejected(
                "invalid_parameters",
                "The parameters do not satisfy the step's parameter schema.",
                details={
                    "problem_count": len(exc.problems),
                    "first_problem": exc.problems[0][:200],
                },
            ) from None
        self.log.info("parameters: %s", self.params)

        inputs = self._stage_inputs()
        bundles = self._mount_bundles()
        self.ctx = StepContext(
            manifest=m,
            params=self.params,
            in_dir=self.in_dir,
            out_dir=self.out_dir,
            tmp_dir=Path(self.environ.get("TMPDIR", "/tmp")),
            inputs=inputs,
            bundles=bundles,
            cpus=_env.cpus(self.environ),
            memory_mb=_int_or_none(self.environ.get("STEP_MEMORY_MB")),
            gpus=_int_or_none(self.environ.get("STEP_GPUS")) or 0,
            logger=self.log,
        )
        self.ctx.add_software("compneuro-step-sdk", __version__)
        self.ctx.add_software("python", platform.python_version())

    def _stage_inputs(self) -> dict[str, Path | None]:
        m = self.manifest
        staged: dict[str, Path | None] = {}
        for port in m.inputs:
            path = self.in_dir / port.filename
            if path.is_file():
                staged[port.name] = path
                self.input_records.append(file_record(port.name, port.type, path))
            else:
                staged[port.name] = None
        known = {p.filename for p in m.inputs} | {"params.json"}
        for entry in sorted(self.in_dir.iterdir()):
            if entry.name not in known:
                self.log.warning(
                    "ignoring unexpected file '%s' in the input directory", entry.name
                )
        for port in m.inputs:
            if staged[port.name] is None and port.required:
                raise InputRejected(
                    "input_missing",
                    f"The required input '{port.name}' was not provided.",
                    port=port.name,
                )
        for port in m.inputs:
            path = staged[port.name]
            if path is None:
                continue
            problems = formats.check_file(path, port.type)
            if problems:
                for p in problems:
                    self.log.error(
                        "input '%s' (%s): %s", port.name, port.type, p.message
                    )
                first = problems[0]
                details: dict[str, Any] = {
                    "type": port.type,
                    "problem_count": len(problems),
                }
                details.update(first.details)
                raise InputRejected(
                    "input_format_invalid",
                    f"The input '{port.name}' is not a valid {port.type} file. {first.message}",
                    port=port.name,
                    details=details,
                )
            self.log.info("input '%s' (%s) present and valid", port.name, port.type)
        return staged

    def _mount_bundles(self) -> dict[str, Path]:
        validate = contracts.validator()
        mounted: dict[str, Path] = {}
        for req in self.manifest.bundles:
            name = req["name"]
            directory = self.ref_dir / name
            doc_path = directory / "bundle.yaml"
            if not doc_path.is_file():
                if req.get("required", True):
                    raise InternalFailure(
                        "bundle_missing",
                        f"The required bundle '{name}' is not mounted.",
                    )
                self.log.info("optional bundle '%s' is not mounted", name)
                continue
            doc = yaml.safe_load(doc_path.read_text(encoding="utf-8"))
            if validate.schema_errors(doc, "bundle"):
                raise InternalFailure(
                    "bundle_invalid", f"The bundle '{name}' has an invalid bundle.yaml."
                )
            if doc["type"] != req["type"]:
                raise InternalFailure(
                    "bundle_mismatch",
                    f"The bundle '{name}' has type '{doc['type']}', expected '{req['type']}'.",
                )
            if "id" in req and doc["id"] != req["id"]:
                raise InternalFailure(
                    "bundle_mismatch",
                    f"The bundle '{name}' is not the required bundle id.",
                )
            if "version" in req and not validate.satisfies(
                doc["version"], req["version"]
            ):
                raise InternalFailure(
                    "bundle_mismatch",
                    f"The bundle '{name}' version does not satisfy the requirement.",
                )
            mounted[name] = directory
            self.bundle_records.append(
                {
                    "name": name,
                    "id": doc["id"],
                    "version": doc["version"],
                    "content_digest": doc["content_digest"],
                }
            )
            self.log.info("bundle '%s' %s %s mounted", name, doc["id"], doc["version"])
        return mounted

    # --- outcomes -------------------------------------------------------------

    def _check_outputs(self) -> list[dict]:
        m = self.manifest
        allowed = {p.filename for p in m.outputs} | {EXTRA_DIR}
        strays = []
        for entry in sorted(self.out_dir.iterdir()):
            if entry.name not in allowed:
                strays.append(entry.name)
            elif entry.name == EXTRA_DIR:
                if not entry.is_dir():
                    strays.append(entry.name)
            elif not entry.is_file():
                strays.append(entry.name)
        if strays:
            self.log.error(
                "files not allowed in the output directory: %s", ", ".join(strays)
            )
            raise InternalFailure(
                "unexpected_output_file",
                "The step left files in the output directory that the contract does not allow.",
                {"count": len(strays)},
            )
        records = []
        for port in m.outputs:
            path = self.out_dir / port.filename
            if not path.is_file():
                if port.required:
                    raise InternalFailure(
                        "missing_required_output",
                        f"The step did not produce the required output '{port.name}'.",
                        {"output_port": port.name},
                    )
                continue
            problems = formats.check_file(path, port.type)
            if problems:
                for p in problems:
                    self.log.error(
                        "output '%s' (%s): %s", port.name, port.type, p.message
                    )
                raise InternalFailure(
                    "invalid_output",
                    f"The output '{port.name}' is not a valid {port.type} file. {problems[0].message}",
                    {"output_port": port.name, "type": port.type},
                )
            records.append(file_record(port.name, port.type, path))
        return records

    def _provenance(self, status: str, outputs: list[dict]) -> dict:
        ctx = self.ctx
        return provenance.build_provenance(
            self.manifest,
            status,
            self.started_at,
            self.params,
            self.input_records,
            outputs,
            self.bundle_records,
            ctx.software if ctx else _base_software(),
            ctx.warnings if ctx else [],
        )

    def _succeed(self) -> int:
        outputs = self._check_outputs()
        record = self._provenance("succeeded", outputs)
        problems = provenance.record_problems(record, "provenance")
        if not problems:
            problems = contracts.validator()._provenance_vs_step(
                record, self.manifest.data
            )
        if problems:
            self.log.error("provenance self-check failed: %s", "; ".join(problems))
            raise InternalFailure(
                "invalid_provenance",
                "The provenance record is invalid.",
                {"problem_count": len(problems)},
            )
        provenance.write_json(self.out_dir / PROVENANCE_FILE, record)
        self.log.info(
            "finished: %d output(s), %d warning(s)",
            len(outputs),
            len(record.get("warnings", [])),
        )
        return EXIT_OK

    def _reject(self, exc: StepRejection) -> int:
        m = self.manifest
        declared = m.errors.get(exc.code)
        if declared is None:
            self.log.error("undeclared error code '%s'", exc.code)
            return self._internal(
                "undeclared_error_code",
                f"The step tried to report the undeclared error code '{exc.code}'.",
            )
        if declared["category"] != exc.category:
            return self._internal(
                "error_category_mismatch",
                f"The error code '{exc.code}' is declared with another category.",
            )
        if exc.port is not None and exc.port not in {p.name for p in m.inputs}:
            return self._internal(
                "invalid_error_report", "The error names an unknown input port."
            )
        error = provenance.build_error(
            m, exc.category, exc.code, exc.message, exc.port, exc.details
        )
        problems = provenance.record_problems(error, "error")
        if problems:
            self.log.error("invalid error report: %s", "; ".join(problems))
            return self._internal(
                "invalid_error_report", "The step produced an invalid error report."
            )
        self.log.error("%s (%s): %s", exc.category, exc.code, exc.message)
        return self._write_failure(error, "rejected", EXIT_REJECTED)

    def _internal(
        self,
        code: str,
        message: str,
        details: dict | None = None,
        exit_code: int = EXIT_INTERNAL,
    ) -> int:
        error = provenance.build_error(
            self.manifest, "internal", code, message[:500], None, details
        )
        return self._write_failure(error, "failed", exit_code)

    def _write_failure(self, error: dict, status: str, exit_code: int) -> int:
        """Best effort: remove output ports, write error.json, then provenance.json."""
        try:
            for port in self.manifest.outputs:
                (self.out_dir / port.filename).unlink(missing_ok=True)
            provenance.write_json(self.out_dir / ERROR_FILE, error)
            record = self._provenance(status, [])
            provenance.write_json(self.out_dir / PROVENANCE_FILE, record)
        except Exception:
            self.log.error("could not write the failure report", exc_info=True)
            return EXIT_INTERNAL if exit_code == EXIT_REJECTED else exit_code
        return exit_code


def _base_software() -> list[dict]:
    return [{"name": "compneuro-step-sdk", "version": __version__}]


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _install_signal_handlers() -> Callable[[], None]:
    previous: dict[int, Any] = {}

    def handler(signum, _frame):
        signal.signal(signum, signal.SIG_DFL)  # a second signal terminates at once
        raise Terminated(signum)

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, handler)
        except ValueError:  # not in the main thread
            pass

    def restore() -> None:
        for sig, old in previous.items():
            try:
                signal.signal(sig, old)
            except ValueError:
                pass

    return restore


def run_step(
    run_fn: RunFunction,
    in_dir: Path | str = "/in",
    out_dir: Path | str = "/out",
    params_path: Path | str | None = None,
    manifest_path: Path | str = DEFAULT_MANIFEST_PATH,
    ref_dir: Path | str = "/ref",
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run ``run_fn`` as a contract step and return the process exit code.

    Writes ``provenance.json`` and, on failure, ``error.json`` into ``out_dir``.
    """
    logger = logging_setup.setup()
    in_dir, out_dir = Path(in_dir), Path(out_dir)
    params = Path(params_path) if params_path else in_dir / "params.json"
    env = os.environ if environ is None else environ
    try:
        manifest = load_manifest(manifest_path)
    except ManifestError as exc:
        logger.error("%s", exc)
        return EXIT_INTERNAL
    restore = _install_signal_handlers()
    try:
        return _Run(
            run_fn, manifest, in_dir, out_dir, params, Path(ref_dir), env, logger
        ).execute()
    finally:
        restore()


__all__ = ["EXIT_INTERNAL", "EXIT_OK", "EXIT_REJECTED", "EXIT_TERMINATED", "run_step"]
