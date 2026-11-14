"""Load and validate pipeline definitions (docs/contracts/pipeline.md, card F0-15).

A ``PipelineDefinition`` is an immutable, validated view of one ``pipeline.yaml``:
the exact bytes' SHA-256 (recorded as ``pipeline_runs.pipeline_sha256``), the
slots in display order and in dependency order, and helpers for conditions and
sources. Validation reuses ``contracts/validate.py`` (schema, single-document
rules including the static availability check of pipeline.md section 3.3), so
the orchestrator refuses exactly what the contract validator refuses.

No database imports here.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from contracts import validate as contract


class PipelineError(ValueError):
    """A pipeline definition or step manifest is invalid or inconsistent."""


# --------------------------------------------------------------------- artefact types


@lru_cache(maxsize=1)
def artefact_types() -> tuple[str, dict[str, Any]]:
    """(registry version, types) of ``contracts/artefact-types.yaml``."""
    data = contract.load_file(contract.DEFAULT_TYPES_PATH)
    return str(data.get("version")), dict(data.get("types", {}))


@lru_cache(maxsize=1)
def artefact_grids() -> dict[str, Any]:
    """Named voxel grids (``{space, shape, affine}``) of the same registry."""
    data = contract.load_file(contract.DEFAULT_TYPES_PATH)
    return dict(data.get("grids", {}))


def extension_for(artefact_type: str) -> str:
    _version, types = artefact_types()
    try:
        return types[artefact_type]["extension"]
    except KeyError as exc:
        raise PipelineError(f"unknown artefact type '{artefact_type}'") from exc


def staged_name(port: str, artefact_type: str) -> str:
    """File name under ``/in`` or ``/out``: port name plus the type's extension."""
    return port + extension_for(artefact_type)


# --------------------------------------------------------------------------- sources


@dataclass(frozen=True)
class Source:
    """One entry of ``consumes[].from`` (pipeline.md section 3.1)."""

    kind: str  # "input" | "slot" | "run"
    name: str  # input name, slot name, or "provenance"
    port: str | None = None

    @classmethod
    def parse(cls, text: str) -> Source:
        head, _, tail = text.partition(".")
        if head == "inputs":
            return cls("input", tail)
        if head == "run":
            return cls("run", tail)
        return cls("slot", head, tail)


def sources_of(consume: Mapping[str, Any]) -> list[Source]:
    raw = consume["from"]
    return [Source.parse(s) for s in (raw if isinstance(raw, list) else [raw])]


def condition_holds(cond: Mapping[str, Any] | None, present: Iterable[str]) -> bool:
    """``when`` clauses: all ``inputs_present`` present and all ``inputs_absent`` absent."""
    if not cond:
        return True
    present = set(present)
    return all(i in present for i in cond.get("inputs_present", [])) and not any(
        i in present for i in cond.get("inputs_absent", [])
    )


# ------------------------------------------------------------------------ definition


@dataclass(frozen=True)
class PipelineDefinition:
    data: Mapping[str, Any]
    sha256: str

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def version(self) -> str:
        return self.data["version"]

    @property
    def inputs(self) -> dict[str, str]:
        """Run input name -> artefact type."""
        return {i["name"]: i["type"] for i in self.data["inputs"]}

    @property
    def slots(self) -> list[Mapping[str, Any]]:
        """Slots in pipeline (display) order; the index is ``run_slots.position``."""
        return list(self.data["slots"])

    def slot(self, name: str) -> Mapping[str, Any]:
        for slot in self.data["slots"]:
            if slot["name"] == name:
                return slot
        raise PipelineError(f"pipeline {self.id} has no slot '{name}'")

    @property
    def topological_order(self) -> list[str]:
        """Slot names so that every slot comes after the slots it consumes from;
        ties keep pipeline order."""
        names = [s["name"] for s in self.data["slots"]]
        deps = {
            s["name"]: {
                src.name
                for c in s["consumes"]
                for src in sources_of(c)
                if src.kind == "slot"
            }
            for s in self.data["slots"]
        }
        order: list[str] = []
        while len(order) < len(names):
            for name in names:
                if name not in order and deps[name] <= set(order):
                    order.append(name)
                    break
            else:  # pragma: no cover - validation rejects cycles
                raise PipelineError("slot wiring has a cycle")
        return order

    def match_input_set(self, roles: Iterable[str]) -> str:
        """Name of the input set whose inputs equal ``roles`` (pipeline.md section 2)."""
        wanted = set(roles)
        for input_set in self.data["input_sets"]:
            if set(input_set["inputs"]) == wanted:
                return input_set["name"]
        raise PipelineError(
            f"the inputs {sorted(wanted)} match no input set of pipeline {self.id}"
        )

    def implementation(self, slot: str, implementation_id: str) -> Mapping[str, Any]:
        for impl in self.slot(slot)["implementations"]["allowed"]:
            if impl["id"] == implementation_id:
                return impl
        raise PipelineError(
            f"implementation '{implementation_id}' is not allowed in slot '{slot}'"
        )

    @property
    def images(self) -> frozenset[str]:
        """Every pinned image: the docker executor's allow-list (F0-14)."""
        return frozenset(
            impl["image"]
            for slot in self.data["slots"]
            for impl in slot["implementations"]["allowed"]
        )


def step_identity_errors(data: Mapping[str, Any]) -> list[str]:
    """A step name and version may be allowed in one slot only.

    The case store (card F0-17, ``app.services.bids``) keys a run's derivatives
    by step, not by slot: ``<run>/derivatives/<step name>-<step version>/``.
    Two slots running the same step version in one run would write the same
    BIDS file names and the same ``provenance.json`` there; the second slot
    silently replaces the first slot's files, whose artefact rows then point at
    bytes that no longer match their SHA-256. Making the path slot-scoped is a
    case-store change (F0-17); until then such a pipeline is refused when it is
    loaded. The same step may appear several times *within* one slot
    (configured variants): only one of them is chosen per run.
    """
    owners: dict[tuple[str, str], str] = {}
    errors = []
    for slot in data.get("slots", []):
        identities = {
            (impl["step"]["name"], impl["step"]["version"])
            for impl in slot["implementations"]["allowed"]
        }
        for identity in sorted(identities):
            first = owners.setdefault(identity, slot["name"])
            if first != slot["name"]:
                errors.append(
                    f"step {identity[0]} {identity[1]} is allowed in slots "
                    f"'{first}' and '{slot['name']}'; their outputs would share "
                    f"derivatives/{identity[0]}-{identity[1]}/ in the case store"
                )
    return errors


def load_pipeline(source: bytes | str | Path) -> PipelineDefinition:
    """Parse and validate a ``pipeline.yaml``; raise ``PipelineError`` listing every problem.

    ``source`` is a path or the file's bytes. The SHA-256 is computed over the
    exact bytes, as pipeline_runs.pipeline_sha256 requires.
    """
    raw = Path(source).read_bytes() if isinstance(source, (str, Path)) else source
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise PipelineError(f"cannot parse pipeline: {exc}") from exc
    if not isinstance(data, dict) or data.get("kind") != "Pipeline":
        raise PipelineError("not a pipeline definition (kind must be 'Pipeline')")
    errors = contract.schema_errors(data, "pipeline")
    if not errors:
        doc = contract.Document(Path("pipeline.yaml"), data, "pipeline")
        _version, types = artefact_types()
        errors = contract.check_contract_version(doc) + contract.check_pipeline(
            data, types
        )
        errors += step_identity_errors(data)
    if errors:
        raise PipelineError("invalid pipeline: " + "; ".join(errors))
    return PipelineDefinition(data=data, sha256=hashlib.sha256(raw).hexdigest())


def manifest_errors(manifest: Mapping[str, Any]) -> list[str]:
    """Schema and single-document errors of a step manifest (step-contract.md 1.2)."""
    errors = contract.schema_errors(manifest, "step")
    if errors:
        return errors
    doc = contract.Document(Path("step.yaml"), manifest, "step")
    _version, types = artefact_types()
    return contract.check_contract_version(doc) + contract.check_step(
        dict(manifest), types
    )


def check_implementation(
    pipeline: PipelineDefinition, slot: str, implementation_id: str, manifest: Mapping
) -> None:
    """Raise ``PipelineError`` unless ``manifest`` can fill the slot (M1-M5, 4.1)."""
    impl = pipeline.implementation(slot, implementation_id)
    errors = manifest_errors(manifest)
    if not errors:
        if (manifest["name"], manifest["version"]) != (
            impl["step"]["name"],
            impl["step"]["version"],
        ):
            errors.append("manifest name or version differs from the implementation")
        errors += contract.slot_match_errors(dict(pipeline.slot(slot)), dict(manifest))
    if errors:
        raise PipelineError(
            f"step {impl['step']['name']} {impl['step']['version']} cannot fill slot "
            f"'{slot}': " + "; ".join(errors)
        )
