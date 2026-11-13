"""Parameter resolution (contract 1.4): defaults, validation, reserved ``seed``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import contracts


class ParameterError(Exception):
    """The parameter file is unreadable or violates the manifest schema."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def read_params_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ParameterError(["the parameter file is missing"]) from None
    except (OSError, UnicodeDecodeError):
        raise ParameterError(["the parameter file cannot be read"]) from None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ParameterError(["the parameter file is not valid JSON"]) from None
    if not isinstance(data, dict):
        raise ParameterError(["the parameter file is not a JSON object"])
    return data


def resolve(schema: dict, supplied: dict[str, Any]) -> dict[str, Any]:
    """Schema defaults overridden by ``supplied``, validated against ``schema``.

    The orchestrator writes a complete file, so defaults only matter for runs outside
    the platform; keys the schema does not declare are rejected.
    """
    validate = contracts.validator()
    values = validate.effective_parameters(schema, supplied)
    problems = validate.parameter_errors(schema, values)
    if problems:
        raise ParameterError(problems)
    return values
