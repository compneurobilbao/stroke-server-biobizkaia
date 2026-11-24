"""Synthetic example inputs a deployment may offer (opt-in, off by default).

``EXAMPLE_INPUTS_DIR`` points at a directory holding ``manifest.json`` and the example
files it lists. When the setting is empty (the default) the feature is off: the list is
empty and attaching an example is refused. An attached example is stored through the
normal input path and carries the reserved origin ``external-tool`` / ``EXAMPLE_TOOL``,
so it is shown as a synthetic example wherever the declared origin appears.

Manifest format (``version`` 1)::

    {"version": 1, "examples": [
        {"id": "...", "role": "lesion_mni", "label": "...", "description": "...",
         "licence": "CC0-1.0", "file": "name.nii.gz"}]}

Never put real or patient-derived data in an example directory.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.api.v1.errors import v1_error
from app.core.config import Settings

log = logging.getLogger(__name__)

EXAMPLE_TOOL = "stroke-server-example"
MANIFEST_NAME = "manifest.json"
_ID = re.compile(r"^[A-Za-z0-9._+-]{1,64}$")
_ROLE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_TEXT_MAX = 500


@dataclass(frozen=True)
class ExampleInput:
    id: str
    role: str
    label: str
    description: str
    licence: str
    path: Path


def is_example_origin(origin: object) -> bool:
    return (
        isinstance(origin, dict)
        and origin.get("kind") == "external-tool"
        and origin.get("tool") == EXAMPLE_TOOL
    )


def example_origin(example: ExampleInput) -> dict:
    return {"kind": "external-tool", "tool": EXAMPLE_TOOL, "version": example.id}


def examples_enabled(settings: Settings) -> bool:
    return bool(settings.example_inputs_dir.strip())


def _entry(root: Path, raw: object) -> ExampleInput | None:
    if not isinstance(raw, dict):
        return None
    fields = {
        k: raw.get(k) for k in ("id", "role", "label", "description", "licence", "file")
    }
    if not all(isinstance(v, str) and v for v in fields.values()):
        return None
    if not _ID.match(fields["id"]) or not _ROLE.match(fields["role"]):
        return None
    if any(len(fields[k]) > _TEXT_MAX for k in ("label", "description", "licence")):
        return None
    name = fields["file"]
    if Path(name).name != name or name.startswith("."):
        return None
    path = root / name
    if path.is_symlink() or not path.is_file():
        return None
    return ExampleInput(
        id=fields["id"],
        role=fields["role"],
        label=fields["label"],
        description=fields["description"],
        licence=fields["licence"],
        path=path,
    )


def load_examples(settings: Settings) -> list[ExampleInput]:
    """The valid entries of the manifest; empty when the feature is off or the manifest
    is missing or unreadable (a broken entry is skipped and logged)."""
    if not examples_enabled(settings):
        return []
    root = Path(settings.example_inputs_dir)
    try:
        data = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
        raw_entries = data["examples"]
        if data.get("version") != 1 or not isinstance(raw_entries, list):
            raise ValueError("unsupported manifest")
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("Example inputs enabled but the manifest is unusable")
        return []
    out: list[ExampleInput] = []
    seen: set[str] = set()
    for raw in raw_entries:
        entry = _entry(root, raw)
        if entry is None or entry.id in seen:
            log.warning("Skipping an invalid or duplicate example input entry")
            continue
        seen.add(entry.id)
        out.append(entry)
    return out


def find_example(settings: Settings, example_id: str) -> ExampleInput:
    if not examples_enabled(settings):
        raise v1_error(
            404, "example_inputs_disabled", "Example inputs are not enabled here"
        )
    for entry in load_examples(settings):
        if entry.id == example_id:
            return entry
    raise v1_error(404, "example_not_found", "Example input not found")
