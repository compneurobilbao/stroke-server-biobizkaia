"""Where the orchestrator finds pipeline definitions, step manifests and presets
(cards F0-15 and F1-19).

Small protocols, so that the pipeline registry of card F0-33 (manifests read from
images by digest, step-contract.md 1.1) can replace the interim directory
implementations without touching the orchestrator:

``PipelineRegistry.get(pipeline_id, version) -> PipelineDefinition | None``
``StepCatalog.manifest(name, version, image) -> Mapping | None``
``PresetRegistry.get(pipeline_id, pipeline_version, preset_id, version=None)
-> PresetDefinition | None`` and ``PresetRegistry.presets(pipeline_id,
pipeline_version) -> list[PresetDefinition]``

The directory implementations scan a tree for ``pipeline.yaml``, ``step.yaml`` and
``presets/<id>.yaml`` files (configured with ``PIPELINES_DIR`` and
``STEP_MANIFESTS_DIR``; presets live next to their pipeline, presets.md section 1).
Invalid files are logged and skipped, never half-loaded.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import yaml

from contracts import validate as contract
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


# ----------------------------------------------------------------------------- presets


class PresetError(ValueError):
    """A preset file that cannot be offered (rules P1 and P7, presets.md section 3)."""


@dataclass(frozen=True)
class PresetDefinition:
    """An immutable, validated view of one preset file (contract 1.2, presets.md).

    ``sha256`` is the SHA-256 of the exact file bytes, recorded in runs as
    ``pipeline_runs.preset_sha256`` (rule P8)."""

    data: Mapping[str, Any]
    sha256: str

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def version(self) -> str:
        return self.data["version"]

    @property
    def pipeline_id(self) -> str:
        return self.data["pipeline"]["id"]

    @property
    def pipeline_versions(self) -> str:
        return self.data["pipeline"]["versions"]

    @property
    def input_sets(self) -> list[str]:
        return list(self.data["input_sets"])

    @property
    def required(self) -> list[str]:
        return list(self.data["targets"]["required"])

    @property
    def if_available(self) -> list[str]:
        return list(self.data["targets"].get("if_available", []))

    def choice(self, slot: str) -> tuple[str, str] | None:
        """``("fixed" | "default", implementation id)`` for ``slot``, or None."""
        entry = self.data.get("implementations", {}).get(slot)
        if not entry:
            return None
        kind = "fixed" if "fixed" in entry else "default"
        return kind, entry[kind]


def load_preset(source: bytes | str | Path) -> PresetDefinition:
    """Parse a preset file and check the rules on the document alone (schema and
    contract version, P1; targets as written, P7). Raise ``PresetError`` listing every
    problem. ``source`` is a path or the file's bytes; the SHA-256 is computed over
    the exact bytes."""
    raw = Path(source).read_bytes() if isinstance(source, (str, Path)) else source
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PresetError(f"cannot parse preset: {exc}") from exc
    if not isinstance(data, dict) or data.get("kind") != "Preset":
        raise PresetError("not a preset (kind must be 'Preset')")
    errors = contract.schema_errors(data, "preset")
    if not errors:
        doc = contract.Document(Path("preset.yaml"), data, "preset", raw)
        errors = contract.check_contract_version(doc) + contract.check_preset(data)
    if errors:
        raise PresetError("invalid preset: " + "; ".join(errors))
    return PresetDefinition(data=data, sha256=contract.preset_sha256(raw))


@runtime_checkable
class PresetRegistry(Protocol):
    def presets(
        self, pipeline_id: str, pipeline_version: str
    ) -> list[PresetDefinition]: ...

    def get(
        self,
        pipeline_id: str,
        pipeline_version: str,
        preset_id: str,
        version: str | None = None,
    ) -> PresetDefinition | None: ...


class StaticPresetRegistry:
    """Presets attached to the loaded pipeline versions (rules P2-P8).

    A preset is attached to every version in ``pipelines`` of its pipeline id that its
    ``pipeline.versions`` range accepts and against which rules P3-P7 hold
    (``contracts.validate.preset_errors``; a fixed stub is detected when ``steps`` has
    the manifest). A version that fails them does not offer the preset; the others
    still do. Rule P8: several files with one identity (pipeline id, preset id,
    version) are all refused, since it cannot be known which one was reviewed; a
    preset whose SHA-256 differs from the one ``recorded`` for its identity (for
    example in earlier runs) is refused, because a changed file needs a new version.
    """

    def __init__(
        self,
        presets: Iterable[PresetDefinition] = (),
        pipelines: PipelineRegistry | None = None,
        steps: StepCatalog | None = None,
        recorded: Mapping[tuple[str, str, str], str] | None = None,
    ) -> None:
        by_identity: dict[tuple[str, str, str], list[PresetDefinition]] = {}
        for preset in presets:
            key = (preset.pipeline_id, preset.id, preset.version)
            by_identity.setdefault(key, []).append(preset)
        self._items: dict[tuple[str, str], dict[tuple[str, str], PresetDefinition]] = {}
        loaded = pipelines.all() if pipelines is not None else []
        for key, found in sorted(by_identity.items()):
            if len(found) > 1:
                log.error(
                    "Ignoring preset %s %s of pipeline %s: defined in %d files (P8)",
                    key[1],
                    key[2],
                    key[0],
                    len(found),
                )
                continue
            preset = found[0]
            known = (recorded or {}).get(key)
            if known is not None and known != preset.sha256:
                log.error(
                    "Ignoring preset %s %s of pipeline %s: the file differs from the "
                    "one recorded for this version; a changed preset needs a new "
                    "version (P8)",
                    key[1],
                    key[2],
                    key[0],
                )
                continue
            for pipeline in loaded:
                if pipeline.id != preset.pipeline_id or not contract.satisfies(
                    pipeline.version, preset.pipeline_versions
                ):
                    continue
                errors = contract.preset_errors(
                    dict(preset.data),
                    dict(pipeline.data),
                    _fixed_manifests(preset, pipeline, steps),
                )
                if errors:
                    log.error(
                        "Preset %s %s is not offered for pipeline %s %s: %s",
                        preset.id,
                        preset.version,
                        pipeline.id,
                        pipeline.version,
                        "; ".join(errors),
                    )
                    continue
                attached = self._items.setdefault((pipeline.id, pipeline.version), {})
                attached[(preset.id, preset.version)] = preset

    def presets(
        self, pipeline_id: str, pipeline_version: str
    ) -> list[PresetDefinition]:
        """Presets attached to one pipeline version, by id and ascending version."""
        attached = self._items.get((pipeline_id, pipeline_version), {})
        return sorted(
            attached.values(),
            key=lambda p: (p.id, contract.parse_semver(p.version)),
        )

    def get(
        self,
        pipeline_id: str,
        pipeline_version: str,
        preset_id: str,
        version: str | None = None,
    ) -> PresetDefinition | None:
        """One attached preset; without ``version``, its highest attached version."""
        candidates = [
            p
            for p in self.presets(pipeline_id, pipeline_version)
            if p.id == preset_id and (version is None or p.version == version)
        ]
        return candidates[-1] if candidates else None


def _fixed_manifests(
    preset: PresetDefinition, pipeline: PipelineDefinition, steps: StepCatalog | None
) -> dict[tuple[str, str], dict]:
    """Manifests of the preset's fixed implementations, for rule P6 (no fixed stub)."""
    out: dict[tuple[str, str], dict] = {}
    if steps is None:
        return out
    for slot in pipeline.slots:
        choice = preset.choice(slot["name"])
        if choice is None or choice[0] != "fixed":
            continue
        for impl in slot["implementations"]["allowed"]:
            if impl["id"] != choice[1]:
                continue
            step = impl["step"]
            manifest = steps.manifest(step["name"], step["version"], impl["image"])
            if manifest is not None:
                out[(step["name"], step["version"])] = dict(manifest)
    return out


class DirectoryPresetRegistry(StaticPresetRegistry):
    """Every valid ``presets/<id>.yaml`` below ``root`` (loaded once), attached to the
    versions of ``pipelines`` as ``StaticPresetRegistry`` describes."""

    def __init__(
        self,
        root: Path | str,
        pipelines: PipelineRegistry,
        steps: StepCatalog | None = None,
        recorded: Mapping[tuple[str, str, str], str] | None = None,
    ) -> None:
        loaded = []
        root = Path(root)
        if root.is_dir():
            for path in sorted(root.rglob("*.yaml")):
                if path.parent.name != "presets":
                    continue
                try:
                    loaded.append(load_preset(path))
                except (OSError, PresetError) as exc:
                    log.error("Ignoring invalid preset %s: %s", path, exc)
        super().__init__(loaded, pipelines, steps, recorded)
