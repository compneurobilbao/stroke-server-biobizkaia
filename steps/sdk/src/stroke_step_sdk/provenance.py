"""Building and writing ``provenance.json`` and ``error.json`` (contract 4 and 5)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import contracts
from .manifest import Manifest

CONTRACT_VERSION = contracts.SUPPORTED_CONTRACT_VERSION


def utc_now() -> str:
    """RFC 3339 UTC timestamp with microseconds and a literal ``Z``."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def build_provenance(
    manifest: Manifest,
    status: str,
    started_at: str,
    parameters: dict[str, Any],
    inputs: list[dict],
    outputs: list[dict],
    bundles: list[dict],
    software: list[dict],
    warnings: list[dict],
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "contract_version": manifest.contract_version,
        "kind": "StepProvenance",
        "step": manifest.step_ref,
        "status": status,
        "started_at": started_at,
        "finished_at": utc_now(),
        "parameters": parameters,
        "inputs": inputs,
        "outputs": outputs,
        "bundles": bundles,
        "software": software,
    }
    if warnings:
        record["warnings"] = warnings
    return record


def build_error(
    manifest: Manifest,
    category: str,
    code: str,
    message: str,
    port: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "contract_version": manifest.contract_version,
        "kind": "StepError",
        "step": manifest.step_ref,
        "category": category,
        "code": code,
        "message": message,
    }
    if port is not None:
        record["port"] = port
    if details:
        record["details"] = details
    return record


def record_problems(record: dict, schema: str) -> list[str]:
    """Schema and single-document rule violations of a record we are about to write."""
    validate = contracts.validator()
    problems = validate.schema_errors(record, schema)
    if problems:
        return problems
    if schema == "error":
        return validate.check_error(record)
    return validate.check_provenance(record)


def write_json(path: Path, record: dict) -> None:
    """Write a JSON file atomically (temporary file in the same directory, then rename)."""
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, sort_keys=False, ensure_ascii=False)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
