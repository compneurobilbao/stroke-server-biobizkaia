"""Where the orchestrator finds pipeline definitions and step manifests (card F0-15).

Two small protocols, so that the pipeline registry of card F0-33 (manifests read
from images by digest, step-contract.md 1.1) can replace the interim directory
implementations without touching the orchestrator:

``PipelineRegistry.get(pipeline_id, version) -> PipelineDefinition | None``
``StepCatalog.manifest(name, version, image) -> Mapping | None``

The directory implementations scan a tree for ``pipeline.yaml`` and ``step.yaml``
files (configured with ``PIPELINES_DIR`` and ``STEP_MANIFESTS_DIR``). Invalid
files are logged and skipped, never half-loaded.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from orchestrator.pipeline import (
    PipelineDefinition,
    PipelineError,
    load_pipeline,
    manifest_errors,
)

log = logging.getLogger(__name__)


@runtime_checkable
class PipelineRegistry(Protocol):
    def get(self, pipeline_id: str, version: str) -> PipelineDefinition | None: ...

    def all(self) -> list[PipelineDefinition]: ...


@runtime_checkable
class StepCatalog(Protocol):
    def manifest(
        self, name: str, version: str, image: str | None = None
    ) -> Mapping[str, Any] | None: ...


class StaticPipelineRegistry:
    def __init__(self, pipelines: Iterable[PipelineDefinition] = ()) -> None:
        self._items = {(p.id, p.version): p for p in pipelines}

    def get(self, pipeline_id: str, version: str) -> PipelineDefinition | None:
        return self._items.get((pipeline_id, version))

    def all(self) -> list[PipelineDefinition]:
        return list(self._items.values())


class DirectoryPipelineRegistry(StaticPipelineRegistry):
    """Every valid ``pipeline.yaml`` below ``root`` (loaded once)."""

    def __init__(self, root: Path | str) -> None:
        loaded = []
        root = Path(root)
        if root.is_dir():
            for path in sorted(root.rglob("pipeline.yaml")):
                try:
                    loaded.append(load_pipeline(path))
                except PipelineError as exc:
                    log.error("Ignoring invalid pipeline %s: %s", path, exc)
        super().__init__(loaded)


class StaticStepCatalog:
    def __init__(self, manifests: Iterable[Mapping[str, Any]] = ()) -> None:
        self._items = {(m["name"], m["version"]): dict(m) for m in manifests}

    def manifest(
        self, name: str, version: str, image: str | None = None
    ) -> Mapping[str, Any] | None:
        return self._items.get((name, version))


class DirectoryStepCatalog(StaticStepCatalog):
    """Every valid ``step.yaml`` below ``root``, keyed by step name and version."""

    def __init__(self, root: Path | str) -> None:
        loaded = []
        root = Path(root)
        if root.is_dir():
            for path in sorted(root.rglob("step.yaml")):
                try:
                    data = yaml.safe_load(path.read_text(encoding="utf-8"))
                except (OSError, yaml.YAMLError) as exc:
                    log.error("Ignoring unreadable step manifest %s: %s", path, exc)
                    continue
                errors = (
                    manifest_errors(data)
                    if isinstance(data, dict)
                    else ["not a mapping"]
                )
                if errors:
                    log.error("Ignoring invalid step manifest %s: %s", path, errors)
                    continue
                loaded.append(data)
        super().__init__(loaded)
