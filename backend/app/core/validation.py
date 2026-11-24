"""Validation levels of pipeline implementations and the deployment's minimum
(card F1-23, ADR-0018 owner decision 8, docs/design/run-plans.md 4.4).

``registry.yaml`` records the level of each implementation (``validation.status``;
``pipelines/stroke-lnm/registry.py`` holds the same rule for the registry check, and a test keeps
the two identical). ``MIN_IMPLEMENTATION_VALIDATION`` is the lowest level a deployment may
enable; the admin endpoint and ``app.cli.seed_implementations`` refuse to enable an
implementation below it with the code ``implementation_not_validated``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)

NOT_VALIDATED_CODE = "implementation_not_validated"
VALIDATION_LEVELS = ("unvalidated", "reference-tested", "validated")
# Profiles whose minimum is fixed to ``validated`` (owner decision 8): a lower value is refused at start.
VALIDATED_ONLY_PROFILES = frozenset({"research-public", "mask-only"})
# Default of every other profile (the lab instance): reference-tested, labelled experimental.
DEFAULT_MIN_VALIDATION = "reference-tested"


def level_rank(level: str) -> int:
    return VALIDATION_LEVELS.index(level)


def implementation_level(entry: Mapping[str, Any]) -> str:
    """Level of a registry entry: its ``validation.status``; ``unvalidated`` when none is
    declared (``registry.py check`` requires one for every real implementation)."""
    declared = (entry.get("validation") or {}).get("status")
    return declared if declared in VALIDATION_LEVELS else "unvalidated"


def min_implementation_validation(
    profile: str, env: Mapping[str, str] | None = None
) -> str:
    """The effective ``MIN_IMPLEMENTATION_VALIDATION`` of a deployment profile.

    Read from the process environment (an empty value counts as unset). On
    ``research-public`` and ``mask-only`` it is ``validated`` and a lower value raises
    ``RuntimeError``; elsewhere an unset value is ``reference-tested`` and any of the three
    levels may be chosen (``unvalidated`` is meant for CI and demo deployments). An
    unknown value always raises.
    """
    env = os.environ if env is None else env
    value = (env.get("MIN_IMPLEMENTATION_VALIDATION") or "").strip()
    if value and value not in VALIDATION_LEVELS:
        raise RuntimeError(
            f"MIN_IMPLEMENTATION_VALIDATION must be one of {list(VALIDATION_LEVELS)}"
        )
    if profile in VALIDATED_ONLY_PROFILES:
        if value not in ("", "validated"):
            raise RuntimeError(
                f"MIN_IMPLEMENTATION_VALIDATION must be validated on the {profile} "
                f"profile (got {value})"
            )
        return "validated"
    return value or DEFAULT_MIN_VALIDATION


def below_minimum(level: str, minimum: str) -> bool:
    return level_rank(level) < level_rank(minimum)


class DirectoryValidationLevels:
    """Levels of the implementations in every ``registry.yaml`` below ``root`` (loaded once),
    keyed by (pipeline id, slot, implementation id). An implementation without a registry
    entry is ``unvalidated``.

    The same registries carry two display-only facts, read here because nothing else reads the
    registry while the API runs: the optional ``title`` of an implementation entry (``title``)
    and the top-level ``in_development`` stages (``in_development``)."""

    def __init__(self, root: Path | str) -> None:
        self._levels: dict[tuple[str, str, str], str] = {}
        self._titles: dict[tuple[str, str, str], str] = {}
        self._stages: dict[str, list[dict[str, Any]]] = {}
        root = Path(root)
        if not root.is_dir():
            return
        for path in sorted(root.rglob("registry.yaml")):
            try:
                doc = yaml.safe_load(path.read_text(encoding="utf-8"))
                pid = doc["pipeline"]["id"]
                for slot, impls in doc["implementations"].items():
                    for impl_id, entry in impls.items():
                        self._levels[(pid, slot, impl_id)] = implementation_level(entry)
                        title = entry.get("title")
                        if isinstance(title, str) and title.strip():
                            self._titles[(pid, slot, impl_id)] = title.strip()
                self._stages[pid] = [
                    {
                        "id": str(stage["id"]),
                        "slot": stage.get("slot"),
                        "title": stage.get("title"),
                        "description": stage.get("description"),
                    }
                    for stage in doc.get("in_development") or []
                ]
            except (OSError, yaml.YAMLError, KeyError, TypeError, AttributeError) as exc:
                log.error("Ignoring unreadable registry %s: %s", path, exc)

    def level(self, pipeline_id: str, slot: str, implementation_id: str) -> str:
        return self._levels.get((pipeline_id, slot, implementation_id), "unvalidated")

    def title(self, pipeline_id: str, slot: str, implementation_id: str) -> str | None:
        """Display title of the registry entry, or None (the step manifest's title applies)."""
        return self._titles.get((pipeline_id, slot, implementation_id))

    def in_development(self, pipeline_id: str) -> list[dict[str, Any]]:
        """Stages the registry lists as in development: ``id`` and, as written there, ``slot``,
        ``title`` and ``description`` (None when absent)."""
        return [dict(stage) for stage in self._stages.get(pipeline_id, [])]


class StaticValidationLevels:
    """Levels given as a mapping (tests); anything else gets ``default``. ``titles`` and
    ``stages`` stand in for the registry's display fields."""

    def __init__(
        self,
        levels: Mapping[tuple[str, str, str], str] | None = None,
        default: str = "unvalidated",
        titles: Mapping[tuple[str, str, str], str] | None = None,
        stages: Mapping[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self._levels = dict(levels or {})
        self._default = default
        self._titles = dict(titles or {})
        self._stages = {k: list(v) for k, v in (stages or {}).items()}

    def level(self, pipeline_id: str, slot: str, implementation_id: str) -> str:
        return self._levels.get((pipeline_id, slot, implementation_id), self._default)

    def title(self, pipeline_id: str, slot: str, implementation_id: str) -> str | None:
        return self._titles.get((pipeline_id, slot, implementation_id))

    def in_development(self, pipeline_id: str) -> list[dict[str, Any]]:
        return [dict(stage) for stage in self._stages.get(pipeline_id, [])]
