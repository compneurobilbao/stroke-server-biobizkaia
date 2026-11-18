"""Seed ``slot_implementation_settings`` and ``preset_settings`` from
``<PIPELINES_DIR>/<id>/registry.yaml`` (cards F0-30a, F1-21 and F1-21a).

    python -m app.cli.seed_implementations --profile deployment|ci [--dry-run] [--reset]

For every pipeline registry found, one row per allowed implementation is written from the
chosen profile (enabled or not, default or not). Defaults:

* Idempotent: a second run changes nothing.
* Existing rows are never touched, so settings changed by an admin through
  ``/api/v1/admin/implementations`` survive a re-run. ``--reset`` puts every row of the
  registry back to the profile.
* A profile that enables a planned implementation is refused, and so is any profile other
  than ``ci`` that enables a stub (the same rules as ``pipelines/stroke-lnm/registry.py check``).
* A profile that enables an implementation below the deployment's minimum validation level
  is refused with ``implementation_not_validated`` (card F1-23, run-plans.md 4.4). The
  minimum is ``unvalidated`` for the ``ci`` profile and otherwise the ``MIN_IMPLEMENTATION_VALIDATION``
  of the deployment (``DEPLOYMENT_PROFILE``: ``validated`` on ``research-public`` and ``mask-only``,
  ``reference-tested`` by default on ``on-premise``). ``--skip-below-minimum`` seeds those
  implementations disabled (and not default) with a warning instead of refusing, so that a
  public deployment can still seed the rest of the profile.
* ``--profile ci`` (stubs, unvalidated implementations) is refused when ``DEPLOYMENT_PROFILE``
  is ``research-public`` or ``mask-only``, unless ``--allow-ci-profile`` is given.
* Already-enabled rows below the minimum (an admin change, or an older seed) are listed as
  warnings and left alone; plans and runs ignore them anyway. ``--reset`` disables them.
* A preset whose required target slot has no implementation enabled by the seeded profile
  (below-minimum and skipped ones do not count) is seeded disabled with a warning. This is a
  first-level check of the target slots only; the API reports the exact availability.
* ``--dry-run`` reports what would change and rolls back.
* Presets follow the same rules. The preset files are found the way the API finds them
  (``orchestrator.catalog.resolve_presets_dir``: ``PRESETS_DIR``, else the pipelines
  directory; ``scan_presets``; file stem equal to the preset ``id``; a file that does not
  load is reported and skipped), so the rows seeded and the presets offered cannot
  diverge. One ``preset_settings`` row per valid preset file of the pipeline is written from ``profiles.<profile>.presets`` (``enabled`` ids and
  the ``default``); existing rows are left to the admin endpoints
  (``/api/v1/admin/presets``) unless ``--reset``. A profile that enables a preset
  without a file, or defaults to one that is not enabled, is refused. With ``--reset``,
  rows of presets whose file no longer exists are disabled (and lose the default flag)
  and logged as ``update``.

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

from app.core.validation import (
    NOT_VALIDATED_CODE,
    VALIDATION_LEVELS,
    below_minimum,
    implementation_level,
    min_implementation_validation,
)
from app.models.preset_setting import PresetSetting
from app.models.reference_bundle import SlotImplementationSetting
from orchestrator.catalog import resolve_presets_dir, scan_presets

PLACEHOLDER_DIGEST = "@sha256:" + "0" * 64
STUB_PROFILES = frozenset({"ci"})
PUBLIC_DEPLOYMENT_PROFILES = frozenset({"research-public", "mask-only"})


class SeedError(Exception):
    pass


def seed_minimum(profile: str, env: dict[str, str] | None = None) -> str:
    """Lowest validation level the seeded ``profile`` may enable: ``unvalidated`` for the ci
    profile, else the deployment's ``MIN_IMPLEMENTATION_VALIDATION``."""
    if profile in STUB_PROFILES:
        return VALIDATION_LEVELS[0]
    environ = os.environ if env is None else env
    deployment = environ.get("DEPLOYMENT_PROFILE") or "research-public"
    try:
        return min_implementation_validation(deployment, environ)
    except RuntimeError as exc:
        raise SeedError(str(exc)) from None


def below_minimum_entries(
    registry: dict[str, Any], profile: str, minimum: str
) -> dict[tuple[str, str], str]:
    """(slot, implementation id) -> level, for the implementations ``profile`` enables
    below ``minimum``."""
    spec = registry["profiles"][profile]
    out: dict[tuple[str, str], str] = {}
    for slot, ids in spec.get("enabled", {}).items():
        for impl_id in ids:
            entry = registry["implementations"].get(slot, {}).get(impl_id)
            if entry is None:
                continue
            level = implementation_level(entry)
            if below_minimum(level, minimum):
                out[(slot, impl_id)] = level
    return out


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


def profile_errors(
    registry: dict[str, Any], profile: str, minimum: str | None = None
) -> list[str]:
    """Rules a profile must satisfy before it may be written to the database. With
    ``minimum`` (a validation level) an enabled implementation below it is an error."""
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
    if minimum is not None:
        for (slot, impl_id), level in below_minimum_entries(
            registry, profile, minimum
        ).items():
            errors.append(
                f"{NOT_VALIDATED_CODE}: {slot}/{impl_id} is {level}, below the "
                f"minimum {minimum} of this deployment"
            )
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


def preset_required_slots_by_pipeline(
    presets_root: Path,
) -> dict[str, dict[str, set[str]]]:
    """pipeline id -> preset id -> slots of the preset's required targets."""
    found, _failed = scan_presets(presets_root)
    out: dict[str, dict[str, set[str]]] = {}
    for _path, preset in found:
        out.setdefault(preset.pipeline_id, {}).setdefault(preset.id, set()).update(
            t.split(".", 1)[0] for t in preset.required
        )
    return out


def preset_ids_by_pipeline(
    presets_root: Path,
) -> tuple[dict[str, list[str]], list[tuple[Path, str]]]:
    """Ids of the valid preset files below ``presets_root`` per pipeline id (sorted), and
    the files that do not load. Same scan as the API's ``DirectoryPresetRegistry``."""
    found, failed = scan_presets(presets_root)
    ids: dict[str, set[str]] = {}
    for _path, preset in found:
        ids.setdefault(preset.pipeline_id, set()).add(preset.id)
    return {pid: sorted(v) for pid, v in ids.items()}, failed


def seed_registry(
    db: Session,
    registry: dict[str, Any],
    profile: str,
    *,
    reset: bool = False,
    preset_ids: list[str] | None = None,
    minimum: str | None = None,
    skip_below_minimum: bool = False,
    preset_required_slots: dict[str, set[str]] | None = None,
) -> list[Change]:
    """Write the rows of one registry (implementations, then presets); the caller commits
    or rolls back. ``preset_ids`` are the preset files of the pipeline (default: the
    presets the profile enables). ``minimum`` is the lowest validation level to enable
    (default: ``seed_minimum(profile)``); below it the profile is refused, or with
    ``skip_below_minimum`` those implementations are seeded disabled."""
    pid = registry["pipeline"]["id"]
    if minimum is None:
        minimum = seed_minimum(profile)
    below = below_minimum_entries(registry, profile, minimum)
    errors = profile_errors(registry, profile, None if skip_below_minimum else minimum)
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
        if key in below and (row is None or reset):  # only with skip_below_minimum
            want = {**want, "enabled": False, "is_default": False}
            changes.append(
                Change(
                    "warning",
                    pid,
                    *key,
                    f"{NOT_VALIDATED_CODE}: {below[key]} is below the minimum "
                    f"{minimum}; seeded disabled",
                )
            )
        elif (
            row is not None
            and row.enabled
            and below_minimum(implementation_level(entry), minimum)
        ):
            changes.append(
                Change(
                    "warning",
                    pid,
                    *key,
                    f"WARNING enabled in the database but {implementation_level(entry)} is "
                    f"below the minimum {minimum}: plans and runs ignore it; run with "
                    "--reset to disable it (admin change, left as is)",
                )
            )
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
    unrunnable: dict[str, str] = {}
    if preset_required_slots:
        usable = {
            slot
            for slot, ids in registry["profiles"][profile].get("enabled", {}).items()
            if any((slot, i) not in below for i in ids)
        }
        for preset_id, slots in preset_required_slots.items():
            missing = sorted(slots - usable)
            if missing:
                unrunnable[preset_id] = (
                    "no implementation is enabled for its required slot(s) "
                    + ", ".join(missing)
                )
    changes += _seed_presets(db, registry, profile, reset, preset_ids, unrunnable)
    return changes


def _seed_presets(
    db: Session,
    registry: dict[str, Any],
    profile: str,
    reset: bool,
    preset_ids: list[str] | None,
    unrunnable: dict[str, str] | None = None,
) -> list[Change]:
    pid = registry["pipeline"]["id"]
    unrunnable = unrunnable or {}
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
    if reset and preset_ids is not None:
        # Settings of a preset whose file no longer exists cannot be offered: disable.
        for key, row in sorted(rows.items()):
            if key in preset_ids or not (row.enabled or row.is_default):
                continue
            row.enabled = False
            row.is_default = False
            changes.append(
                Change(
                    "update",
                    pid,
                    "preset",
                    key,
                    "preset file no longer exists: disabled "
                    f"(was enabled={before[key][0]} default={before[key][1]})",
                )
            )
    taken = any(r.is_default for r in rows.values())
    for want in preset_profile_settings(registry, profile, preset_ids):
        key = want["preset_id"]
        row = rows.get(key)
        if key in unrunnable and want["enabled"] and (row is None or reset):
            want = {**want, "enabled": False, "is_default": False}
            changes.append(
                Change("warning", pid, "preset", key, f"seeded disabled: {unrunnable[key]}")
            )
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
    parser.add_argument(
        "--skip-below-minimum",
        action="store_true",
        help="seed implementations below the deployment's minimum validation level "
        "disabled, with a warning, instead of refusing the profile",
    )
    parser.add_argument(
        "--allow-ci-profile",
        action="store_true",
        help="allow --profile ci (stubs, unvalidated implementations) on a deployment whose "
        "DEPLOYMENT_PROFILE is research-public or mask-only",
    )
    parser.add_argument("--pipeline", help="only this pipeline id")
    parser.add_argument(
        "--presets-dir",
        default=None,
        help="tree scanned for presets/<id>.yaml (default: $PRESETS_DIR, else the "
        "pipelines directory), as the API does",
    )
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
        if args.profile in STUB_PROFILES and not args.allow_ci_profile:
            deployment = os.environ.get("DEPLOYMENT_PROFILE") or "research-public"
            if deployment in PUBLIC_DEPLOYMENT_PROFILES:
                raise SeedError(
                    f"profile {args.profile!r} enables stubs and unvalidated implementations "
                    f"and is refused on a {deployment} deployment; "
                    "use --allow-ci-profile to override"
                )
        registries = (
            find_registry_files(root, args.pipeline) if root.is_dir() else []
        )
        if not registries:
            raise SeedError(f"no registry.yaml found under {root}")
        presets_root = (
            Path(args.presets_dir)
            if args.presets_dir
            else resolve_presets_dir(root)
        )
        preset_ids, preset_failures = preset_ids_by_pipeline(presets_root)
        required_slots = preset_required_slots_by_pipeline(presets_root)
        for bad, message in preset_failures:
            print(f"warning: preset {bad} ignored: {message}", file=sys.stderr)
        db = session_factory()
        try:
            changes: list[Change] = []
            for _path, registry in registries:
                changes += seed_registry(
                    db,
                    registry,
                    args.profile,
                    reset=args.reset,
                    preset_ids=preset_ids.get(registry["pipeline"]["id"], []),
                    skip_below_minimum=args.skip_below_minimum,
                    preset_required_slots=required_slots.get(registry["pipeline"]["id"]),
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
