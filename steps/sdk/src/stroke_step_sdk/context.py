"""The object a step's ``run`` function receives."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .errors import InternalFailure
from .manifest import Manifest

SNAKE_ID = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")


class StepContext:
    """Inputs, parameters, output locations and reporting hooks for one step run.

    ``inputs`` holds a path for every staged input port and ``None`` for optional ports
    that are absent. Required ports are guaranteed to be present and valid when ``run``
    starts. Output files are written at ``output_path(port)``; the SDK checks and hashes
    them after ``run`` returns.
    """

    def __init__(
        self,
        manifest: Manifest,
        params: dict[str, Any],
        in_dir: Path,
        out_dir: Path,
        tmp_dir: Path,
        inputs: dict[str, Path | None],
        bundles: dict[str, Path],
        cpus: int,
        memory_mb: int | None,
        gpus: int,
        logger: logging.Logger,
    ) -> None:
        self.manifest = manifest
        self.params = params
        self.in_dir = in_dir
        self.out_dir = out_dir
        self.tmp_dir = tmp_dir
        self.inputs = inputs
        self.bundles = bundles
        self.cpus = cpus
        self.memory_mb = memory_mb
        self.gpus = gpus
        self.log = logger
        self.warnings: list[dict[str, str]] = []
        self.software: list[dict[str, str]] = []

    # --- inputs and bundles -------------------------------------------------

    def input_path(self, port: str) -> Path:
        """Path of a staged input; raises if an optional input is absent."""
        if port not in self.inputs:
            raise KeyError(f"unknown input port '{port}'")
        path = self.inputs[port]
        if path is None:
            raise FileNotFoundError(f"optional input '{port}' is not available")
        return path

    def has_input(self, port: str) -> bool:
        return self.inputs.get(port) is not None

    def bundle_path(self, name: str) -> Path:
        """Directory of a mounted bundle (``/ref/<name>``)."""
        if name not in self.bundles:
            raise KeyError(f"bundle '{name}' is not mounted")
        return self.bundles[name]

    # --- outputs ---------------------------------------------------------------

    def output_path(self, port: str) -> Path:
        """Where to write the file of an output port: ``/out/<port><ext>``."""
        return self.out_dir / self.manifest.output_port(port).filename

    def extra_path(self, name: str) -> Path:
        """A file in ``/out/extra/`` for undeclared diagnostics (directory is created)."""
        if "/" in name or "\\" in name or name in ("", ".", ".."):
            raise ValueError("extra file names must be plain file names")
        directory = self.out_dir / "extra"
        directory.mkdir(exist_ok=True)
        return directory / name

    # --- parameters ----------------------------------------------------------

    @property
    def seed(self) -> int:
        if "seed" not in self.params:
            raise InternalFailure(
                "seed_not_declared", "The step uses a seed but does not declare one."
            )
        return int(self.params["seed"])

    def rng(self):
        """A numpy generator derived from the reserved ``seed`` parameter."""
        import numpy as np

        return np.random.default_rng(self.seed)

    # --- reporting ---------------------------------------------------------------

    def warn(self, code: str, message: str, port: str | None = None) -> None:
        """Record a non-fatal finding in ``provenance.json`` (and log it)."""
        if not SNAKE_ID.match(code) or len(code) > 64:
            raise ValueError("warning code must be snake_case, at most 64 characters")
        if not message or len(message) > 500:
            raise ValueError("warning message must be 1-500 characters")
        record = {"code": code, "message": message}
        if port is not None:
            known = {p.name for p in (*self.manifest.inputs, *self.manifest.outputs)}
            if port not in known:
                raise ValueError(f"unknown port '{port}'")
            record["port"] = port
        self.warnings.append(record)
        self.log.warning("%s: %s", code, message)

    def add_software(self, name: str, version: str) -> None:
        """Declare a tool or library that determines results (provenance ``software``)."""
        record = {"name": name, "version": version}
        if record not in self.software:
            self.software.append(record)
