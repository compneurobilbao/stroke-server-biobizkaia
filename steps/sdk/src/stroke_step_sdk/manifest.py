"""Loading and querying the step manifest (contract section 1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import contracts
from .errors import ManifestError

DEFAULT_MANIFEST_PATH = Path("/opt/step/step.yaml")

# Codes the SDK itself raises with exit 10. A step must declare them in ``errors[]``
# (contract 4.3: the SDK refuses to emit an undeclared code).
SDK_REJECTION_CODES = {
    "input_missing": "input_rejected",
    "input_format_invalid": "input_rejected",
    "invalid_parameters": "input_rejected",
}


@dataclass(frozen=True)
class Port:
    name: str
    type: str
    required: bool
    extension: str
    description: str = ""

    @property
    def filename(self) -> str:
        return f"{self.name}{self.extension}"


@dataclass(frozen=True)
class Manifest:
    data: dict[str, Any]
    path: Path
    inputs: tuple[Port, ...] = field(default=())
    outputs: tuple[Port, ...] = field(default=())

    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def version(self) -> str:
        return self.data["version"]

    @property
    def contract_version(self) -> str:
        return self.data["contract_version"]

    @property
    def parameters_schema(self) -> dict:
        return self.data["parameters"]

    @property
    def bundles(self) -> list[dict]:
        return self.data.get("bundles", [])

    @property
    def errors(self) -> dict[str, dict]:
        return {e["code"]: e for e in self.data.get("errors", [])}

    @property
    def resources(self) -> dict:
        return self.data["resources"]

    @property
    def step_ref(self) -> dict[str, str]:
        return {"name": self.name, "version": self.version}

    def input_port(self, name: str) -> Port:
        for p in self.inputs:
            if p.name == name:
                return p
        raise KeyError(f"unknown input port '{name}'")

    def output_port(self, name: str) -> Port:
        for p in self.outputs:
            if p.name == name:
                return p
        raise KeyError(f"unknown output port '{name}'")

    def missing_sdk_codes(self) -> list[str]:
        declared = self.errors
        return [c for c in SDK_REJECTION_CODES if c not in declared]


def _ports(entries: list[dict], types: dict) -> tuple[Port, ...]:
    return tuple(
        Port(
            name=e["name"],
            type=e["type"],
            required=e.get("required", True),
            extension=types[e["type"]]["extension"],
            description=e.get("description", ""),
        )
        for e in entries
    )


def load_manifest(path: Path | str = DEFAULT_MANIFEST_PATH) -> Manifest:
    """Load ``step.yaml`` and validate it with ``contracts/validate.py``.

    Raises ``ManifestError`` listing every problem found (schema, semantic rules,
    contract version compatibility).
    """
    path = Path(path)
    if not path.is_file():
        raise ManifestError("step manifest not found")
    validate = contracts.validator()
    issues = [
        i
        for i in validate.validate_files([path], kind="step", strict=False)
        if i.severity == "error"
    ]
    if issues:
        raise ManifestError(
            "invalid step manifest: " + "; ".join(i.message for i in issues)
        )
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    types = contracts.registry()["types"]
    return Manifest(
        data=data,
        path=path,
        inputs=_ports(data["inputs"], types),
        outputs=_ports(data["outputs"], types),
    )
