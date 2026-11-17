"""Seed ``slot_implementation_settings`` and ``preset_settings`` from
``<PIPELINES_DIR>/<id>/registry.yaml`` (cards F0-30a and F1-21).

    python -m app.cli.seed_implementations --profile deployment|ci [--dry-run] [--reset]

For every pipeline registry found, one row per allowed implementation is written from the
chosen profile (enabled or not, default or not). Defaults:

* Idempotent: a second run changes nothing.
* Existing rows are never touched, so settings changed by an admin through
  ``/api/v1/admin/implementations`` survive a re-run. ``--reset`` puts every row of the
  registry back to the profile.
* A profile that enables a planned implementation is refused, and so is any profile other
  than ``ci`` that enables a stub (the same rules as ``pipelines/stroke-lnm/registry.py check``).
* ``--dry-run`` reports what would change and rolls back.
* Presets follow the same rules. One ``preset_settings`` row per preset file in
  ``<id>/presets/`` is written from ``profiles.<profile>.presets`` (``enabled`` ids and
  the ``default``); existing rows are left to the admin endpoints
  (``/api/v1/admin/presets``) unless ``--reset``. A profile that enables a preset
  without a file, or defaults to one that is not enabled, is refused.

The row computation (``profile_settings``, ``preset_profile_settings``) generalises
``enabled_settings`` and ``enabled_preset_settings`` of
``pipelines/stroke-lnm/registry.py``; a test keeps them identical.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.preset_setting import PresetSetting
from app.models.reference_bundle import SlotImplementationSetting

PLACEHOLDER_DIGEST = "@sha256:" + "0" * 64
STUB_PROFILES = frozenset({"ci"})


class SeedError(Exception):
    pass


@dataclass(frozen=True)
class Change:
    action: str  # insert | update | unchanged | warning
    pipeline_id: str
    slot: str
    implementation_id: str
    detail: str = ""

    def line(self) -> str:
        text = (
            f"{self.action:<9} {self.pipeline_id}/{self.slot}/{self.implementation_id}"
        )
        return f"{text}  {self.detail}".rstrip()


def profile_settings(registry: dict[str, Any], profile: str) -> list[dict[str, Any]]:
    """One row per implementation: slot, implementation_id, enabled, is_default."""
    try:
        spec = registry["profiles"][profile]
    except KeyError:
        raise SeedError(
            f"profile {profile!r} is not defined "
            f"(have: {sorted(registry.get('profiles', {}))})"
        ) from None
    enabled = spec.get("enabled", {})
    default = spec.get("default", {})
    return [
        {
            "slot": slot,
            "implementation_id": impl_id,
            "enabled": impl_id in enabled.get(slot, []),
            "is_default": default.get(slot) == impl_id,
        }
        for slot, impls in registry["implementations"].items()
        for impl_id in impls
    ]


def preset_profile_settings(
    registry: dict[str, Any], profile: str, preset_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    """One row per preset: preset_id, enabled, is_default. ``preset_ids`` are the presets
    of the pipeline (default: those the profile enables)."""
    profile_settings(registry, profile)  # unknown profile -> SeedError
    spec = registry["profiles"][profile].get("presets", {})
    enabled = spec.get("enabled", [])
    ids = list(enabled) if preset_ids is None else list(preset_ids)
    return [
        {
            "preset_id": preset_id,
            "enabled": preset_id in enabled,
            "is_default": spec.get("default") == preset_id,
        }
        for preset_id in ids
    ]


def preset_profile_errors(
    registry: dict[str, Any], profile: str, preset_ids: list[str]
) -> list[str]:
    errors: list[str] = []
    profile_settings(registry, profile)  # unknown profile -> SeedError
    spec = registry["profiles"][profile].get("presets", {})
    enabled = spec.get("enabled", [])
    errors += [
        f"enabled preset {p!r} has no file" for p in enabled if p not in preset_ids
    ]
    default = spec.get("default")
    if default is not None and default not in enabled:
        errors.append(f"default preset {default!r} is not enabled")
    return errors


def profile_errors(registry: dict[str, Any], profile: str) -> list[str]:
    """Rules a profile must satisfy before it may be written to the database."""
    errors: list[str] = []
    impls = registry["implementations"]
    profile_settings(registry, profile)  # unknown profile -> SeedError
    spec = registry["profiles"][profile]
    for slot, ids in spec.get("enabled", {}).items():
        for impl_id in ids:
            entry = impls.get(slot, {}).get(impl_id)
            if entry is None:
                errors.append(f"unknown implementation {slot}/{impl_id}")
            elif entry["kind"] == "planned":
                errors.append(
                    f"planned implementation {slot}/{impl_id} cannot be enabled"
                )
            elif entry["kind"] == "stub" and profile not in STUB_PROFILES:
                errors.append(
                    f"stub {slot}/{impl_id} cannot be enabled in profile {profile!r}"
                )
    for slot, impl_id in spec.get("default", {}).items():
        if impl_id not in spec.get("enabled", {}).get(slot, []):
            errors.append(f"default {slot}/{impl_id} is not enabled")
    return errors


def find_registries(
    pipelines_dir: Path, only: str | None = None
) -> list[dict[str, Any]]:
    return [doc for _path, doc in find_registry_files(pipelines_dir, only)]


def find_registry_files(
    pipelines_dir: Path, only: str | None = None
) -> list[tuple[Path, dict[str, Any]]]:
    found = []
    for path in sorted(pipelines_dir.glob("*/registry.yaml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or "pipeline" not in doc:
            raise SeedError(f"{path}: not a pipeline registry")
        if only is None or doc["pipeline"]["id"] == only:
            found.append((path, doc))
    return found


def preset_ids_of(registry_path: Path) -> list[str]:
    """Ids (file stems) of the preset files next to a registry, sorted."""
    return sorted(p.stem for p in (registry_path.parent / "presets").glob("*.yaml"))


def seed_registry(
    db: Session,
    registry: dict[str, Any],
    profile: str,
    *,
    reset: bool = False,
    preset_ids: list[str] | None = None,
) -> list[Change]:
    """Write the rows of one registry (implementations, then presets); the caller commits
    or rolls back. ``preset_ids`` are the preset files of the pipeline (default: the
    presets the profile enables)."""
    pid = registry["pipeline"]["id"]
    errors = profile_errors(registry, profile)
    if preset_ids is not None:
        errors += preset_profile_errors(registry, profile, preset_ids)
    if errors:
        raise SeedError(f"{pid}: profile {profile!r} refused: " + "; ".join(errors))
    rows = {
        (r.slot, r.implementation_id): r
        for r in db.scalars(
            select(SlotImplementationSetting).where(
                SlotImplementationSetting.pipeline_id == pid
            )
        )
    }
    before = {key: (r.enabled, r.is_default) for key, r in rows.items()}
    changes: list[Change] = []

    if reset:
        # Free the one-default-per-slot index before rows are rewritten.
        for row in rows.values():
            row.is_default = False
        db.flush()

    taken = {slot for (slot, _), r in rows.items() if r.is_default}
    for want in profile_settings(registry, profile):
        key = (want["slot"], want["implementation_id"])
        row = rows.get(key)
        entry = registry["implementations"][want["slot"]][want["implementation_id"]]
        if want["enabled"] and PLACEHOLDER_DIGEST in entry["image"]:
            changes.append(
                Change(
                    "warning",
                    pid,
                    *key,
                    "enabled, but its image digest is a placeholder: runs cannot "
                    "start until published digests are in the registry",
                )
            )
        if row is None:
            is_default = want["is_default"] and want["slot"] not in taken
            detail = f"enabled={want['enabled']} default={is_default}"
            if want["is_default"] and not is_default:
                detail += " (slot already has another default; kept)"
            if is_default:
                taken.add(want["slot"])
            db.add(
                SlotImplementationSetting(
                    pipeline_id=pid,
                    slot=want["slot"],
                    implementation_id=want["implementation_id"],
                    enabled=want["enabled"],
                    is_default=is_default,
                )
            )
            changes.append(Change("insert", pid, *key, detail))
        elif not reset:
            if row.enabled and entry["kind"] != "real" and profile != "ci":
                changes.append(
                    Change(
                        "warning",
                        pid,
                        *key,
                        f"{entry['kind']} is enabled in the database "
                        "(admin change, left as is)",
                    )
                )
            changes.append(Change("unchanged", pid, *key))
        else:
            is_default = want["is_default"]
            if is_default:
                taken.add(want["slot"])
            old = before[key]
            new = (want["enabled"], is_default)
            row.enabled, row.is_default = new
            if old == new:
                changes.append(Change("unchanged", pid, *key))
            else:
                changes.append(
                    Change(
                        "update",
                        pid,
                        *key,
                        f"enabled {old[0]}->{new[0]}, default {old[1]}->{new[1]}",
                    )
                )
    db.flush()
    changes += _seed_presets(db, registry, profile, reset, preset_ids)
    return changes


def _seed_presets(
    db: Session,
    registry: dict[str, Any],
    profile: str,
    reset: bool,
    preset_ids: list[str] | None,
) -> list[Change]:
    pid = registry["pipeline"]["id"]
    rows = {
        r.preset_id: r
        for r in db.scalars(
            select(PresetSetting).where(PresetSetting.pipeline_id == pid)
        )
    }
    before = {key: (r.enabled, r.is_default) for key, r in rows.items()}
    changes: list[Change] = []
    if reset:
        # Free the one-default-per-pipeline index before rows are rewritten.
        for row in rows.values():
            row.is_default = False
        db.flush()
    taken = any(r.is_default for r in rows.values())
    for want in preset_profile_settings(registry, profile, preset_ids):
        key = want["preset_id"]
        row = rows.get(key)
        if row is None:
            is_default = want["is_default"] and not taken
            detail = f"enabled={want['enabled']} default={is_default}"
            if want["is_default"] and not is_default:
                detail += " (pipeline already has another default; kept)"
            taken = taken or is_default
            db.add(
                PresetSetting(
                    pipeline_id=pid,
                    preset_id=key,
                    enabled=want["enabled"],
                    is_default=is_default,
                )
            )
            changes.append(Change("insert", pid, "preset", key, detail))
        elif not reset:
            changes.append(Change("unchanged", pid, "preset", key))
        else:
            taken = taken or want["is_default"]
            old = before[key]
            new = (want["enabled"], want["is_default"])
            row.enabled, row.is_default = new
            if old == new:
                changes.append(Change("unchanged", pid, "preset", key))
            else:
                changes.append(
                    Change(
                        "update",
                        pid,
                        "preset",
                        key,
                        f"enabled {old[0]}->{new[0]}, default {old[1]}->{new[1]}",
                    )
                )
    db.flush()
    return changes


def main(argv: list[str] | None = None, session_factory=None) -> int:
    parser = argparse.ArgumentParser(
        description="Seed slot implementation settings from pipeline registries"
    )
    parser.add_argument("--profile", required=True, choices=["deployment", "ci"])
    parser.add_argument("--dry-run", action="store_true", help="report and roll back")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="overwrite existing rows (admin changes included) with the profile",
    )
    parser.add_argument("--pipeline", help="only this pipeline id")
    parser.add_argument(
        "--pipelines-dir",
        default=os.environ.get("PIPELINES_DIR", "/app/pipelines"),
        help="directory holding <id>/registry.yaml (default: $PIPELINES_DIR)",
    )
    args = parser.parse_args(argv)

    if session_factory is None:
        from app.db.session import SessionLocal as session_factory

    root = Path(args.pipelines_dir)
    try:
        registries = (
            find_registry_files(root, args.pipeline) if root.is_dir() else []
        )
        if not registries:
            raise SeedError(f"no registry.yaml found under {root}")
        db = session_factory()
        try:
            changes: list[Change] = []
            for path, registry in registries:
                changes += seed_registry(
                    db,
                    registry,
                    args.profile,
                    reset=args.reset,
                    preset_ids=preset_ids_of(path),
                )
            if args.dry_run:
                db.rollback()
            else:
                db.commit()
        finally:
            db.close()
    except SeedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    for change in changes:
        print(change.line())
    counts = {
        a: sum(1 for c in changes if c.action == a)
        for a in ("insert", "update", "unchanged")
    }
    mode = "dry run, nothing written" if args.dry_run else "committed"
    print(
        f"profile={args.profile} reset={args.reset}: {counts['insert']} inserted, "
        f"{counts['update']} updated, {counts['unchanged']} unchanged ({mode})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
