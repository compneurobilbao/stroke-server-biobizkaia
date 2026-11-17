#!/usr/bin/env python3
"""Validate step-contract documents (contract 1.x).

Usage:
    python contracts/validate.py FILE [FILE ...] [--kind KIND] [--types PATH] [--strict]

Each file is validated against its JSON Schema (draft 2020-12), then against the
single-document rules that a schema cannot express, and finally all files given
on one command line are checked against each other (pipeline <-> step manifests
<-> bundles <-> provenance <-> presets). The normative text is docs/contracts/.

The static plan closure of contract 1.2 (``plan_closure``, docs/contracts/presets.md
section 4) is a pure function over a pipeline document, so that the orchestrator can
import it instead of reimplementing it.

Exit status: 0 if every file is valid, 1 if any error was found, 2 on usage errors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

import yaml
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource

CONTRACT_MAJOR = 1
CONTRACT_MINOR = 2
CONTRACT_VERSION = f"{CONTRACT_MAJOR}.{CONTRACT_MINOR}"

CONTRACTS_DIR = Path(__file__).resolve().parent
SCHEMA_DIR = CONTRACTS_DIR / "schemas" / "v1"
DEFAULT_TYPES_PATH = CONTRACTS_DIR / "artefact-types.yaml"

SCHEMA_FILES = {
    "step": "step.schema.json",
    "pipeline": "pipeline.schema.json",
    "bundle": "bundle.schema.json",
    "provenance": "provenance.schema.json",
    "error": "error.schema.json",
    "artefact-types": "artefact-types.schema.json",
    "preset": "preset.schema.json",
}
KIND_TO_SCHEMA = {
    "Step": "step",
    "Pipeline": "pipeline",
    "Bundle": "bundle",
    "StepProvenance": "provenance",
    "RunProvenance": "provenance",
    "StepError": "error",
    "ArtefactTypeRegistry": "artefact-types",
    "Preset": "preset",
}
FILENAME_TO_SCHEMA = {
    "step.yaml": "step",
    "pipeline.yaml": "pipeline",
    "bundle.yaml": "bundle",
    "provenance.json": "provenance",
    "error.json": "error",
    "artefact-types.yaml": "artefact-types",
}
# Values a pipeline may bind to step parameters (docs/contracts/pipeline.md).
BINDING_SAMPLES = {"case.sex": ["female", "male"], "case.age_band": ["65-69"]}
RESERVED_SEED = "seed"
# Contract-level error codes (docs/contracts/step-contract.md section 4.4, contract 1.1).
# Every step whose manifest declares contract 1.1 or later declares them implicitly.
RESERVED_ERROR_CODES = {
    "input_missing": "input_rejected",
    "input_format_invalid": "input_rejected",
    "invalid_parameters": "input_rejected",
    "terminated": "internal",
}
REJECTION_CATEGORIES = ("input_rejected", "qc_failed")
# First minor version of contract major 1 with the 1.1 rules.
RULES_1_1 = 1
# First minor version with presets, run plans, upstream blocks and input origins (ADR-0018).
RULES_1_2 = 2


def contract_minor(value: Any) -> int:
    """Minor part of a ``contract_version`` string (-1 if it cannot be read)."""
    if isinstance(value, str) and re.fullmatch(r"\d+\.\d+", value):
        return int(value.split(".")[1])
    return -1


def rules_1_1(doc: dict) -> bool:
    """True if the document declares contract 1.1 or later. Documents declaring 1.0
    keep the 1.0 rules (no reserved codes, outputs not checked on failure)."""
    return contract_minor(doc.get("contract_version")) >= RULES_1_1


def rules_1_2(doc: dict) -> bool:
    """True if the document declares contract 1.2 or later (fields and values added by
    ADR-0018 are accepted only then)."""
    return contract_minor(doc.get("contract_version")) >= RULES_1_2


def declared_error_codes(step: dict) -> dict[str, str]:
    """Exit-10 codes a step may return, with their category: its own ``errors[]`` plus,
    from contract 1.1, the reserved rejection codes."""
    codes = {e["code"]: e["category"] for e in step.get("errors", [])}
    if rules_1_1(step):
        for code, category in RESERVED_ERROR_CODES.items():
            if category in REJECTION_CATEGORIES:
                codes.setdefault(code, category)
    return codes


@dataclass(frozen=True)
class Issue:
    file: str
    layer: str  # load | schema | semantic | cross
    message: str
    severity: str = "error"  # error | note

    def __str__(self) -> str:
        return f"{self.severity.upper()} {self.file}: [{self.layer}] {self.message}"


@dataclass
class Document:
    path: Path
    data: Any
    schema: str
    raw: bytes | None = (
        None  # exact file bytes, when loaded from a file (preset SHA-256)
    )

    @property
    def kind(self) -> str:
        return self.data.get("kind", "") if isinstance(self.data, dict) else ""


# --------------------------------------------------------------------------- loading


def load_file(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)
    return yaml.safe_load(text)


def detect_schema(data: Any, path: Path, forced: str | None = None) -> str | None:
    if forced:
        return forced
    if isinstance(data, dict) and data.get("kind") in KIND_TO_SCHEMA:
        return KIND_TO_SCHEMA[data["kind"]]
    return FILENAME_TO_SCHEMA.get(path.name)


def _schema_registry() -> Registry:
    resources = []
    for name in SCHEMA_FILES.values():
        contents = json.loads((SCHEMA_DIR / name).read_text(encoding="utf-8"))
        resources.append((contents["$id"], Resource.from_contents(contents)))
    return Registry().with_resources(resources)


_REGISTRY: Registry | None = None


def validator_for(schema: str) -> Draft202012Validator:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _schema_registry()
    contents = json.loads(
        (SCHEMA_DIR / SCHEMA_FILES[schema]).read_text(encoding="utf-8")
    )
    return Draft202012Validator(contents, registry=_REGISTRY)


def schema_errors(data: Any, schema: str) -> list[str]:
    errors = sorted(
        validator_for(schema).iter_errors(data), key=lambda e: list(e.absolute_path)
    )
    out = []
    for err in errors:
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        out.append(f"{where}: {err.message}")
    return out


# --------------------------------------------------------------------------- versions


def parse_semver(text: str) -> tuple[int, int, int, tuple[int, str]]:
    core, _, pre = text.partition("-")
    major, minor, patch = (int(x) for x in core.split("."))
    # A pre-release sorts before the release with the same core.
    return (major, minor, patch, (0, pre) if pre else (1, ""))


def satisfies(version: str, version_range: str) -> bool:
    v = parse_semver(version)
    for raw in version_range.split(","):
        m = re.fullmatch(r"\s*(==|>=|>|<=|<|\^)?\s*(\d+\.\d+\.\d+)\s*", raw)
        if not m:
            return False
        op, ref_text = m.group(1) or "==", m.group(2)
        ref = parse_semver(ref_text)
        ok = {
            "==": v[:3] == ref[:3] and v[3] == ref[3],
            ">=": v >= ref,
            ">": v > ref,
            "<=": v <= ref,
            "<": v < ref,
            "^": v >= ref and v[0] == ref[0],
        }[op]
        if not ok:
            return False
    return True


def check_contract_version(doc: Document) -> list[str]:
    value = doc.data.get("contract_version") if isinstance(doc.data, dict) else None
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+", value):
        return []  # reported by the schema
    major, minor = (int(x) for x in value.split("."))
    if major != CONTRACT_MAJOR:
        return [
            f"contract_version {value}: major version {major} is not supported (supported: {CONTRACT_MAJOR}.x)"
        ]
    if minor > CONTRACT_MINOR:
        return [
            f"contract_version {value} is newer than this validator supports ({CONTRACT_VERSION})"
        ]
    return []


# --------------------------------------------------------------------------- single-document rules


def _duplicates(values: Iterable[str]) -> list[str]:
    seen, dup = set(), []
    for v in values:
        if v in seen and v not in dup:
            dup.append(v)
        seen.add(v)
    return dup


def _external_refs(node: Any) -> list[str]:
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str) and not value.startswith("#"):
                found.append(value)
            else:
                found.extend(_external_refs(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_external_refs(item))
    return found


def parameter_defaults(params_schema: dict) -> dict:
    return {k: v.get("default") for k, v in params_schema.get("properties", {}).items()}


def effective_parameters(params_schema: dict, *overrides: dict) -> dict:
    values = parameter_defaults(params_schema)
    for o in overrides:
        values.update(o or {})
    return values


def parameter_errors(params_schema: dict, values: dict) -> list[str]:
    try:
        validator = Draft202012Validator(params_schema)
        return [
            f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
            for e in validator.iter_errors(values)
        ]
    except SchemaError as exc:  # reported elsewhere
        return [f"parameter schema is invalid: {exc.message}"]


def check_step(data: dict, types: dict) -> list[str]:
    errs: list[str] = []
    inputs = data.get("inputs", [])
    outputs = data.get("outputs", [])
    for label, ports in (("input", inputs), ("output", outputs)):
        for d in _duplicates(p["name"] for p in ports):
            errs.append(f"duplicate {label} port '{d}'")
        for p in ports:
            if p["type"] not in types:
                errs.append(
                    f"{label} port '{p['name']}': unknown artefact type '{p['type']}'"
                )
            elif p["type"] == "run_provenance" and label == "output":
                errs.append(
                    f"output port '{p['name']}': run_provenance is produced by the platform only"
                )

    params = data.get("parameters", {})
    try:
        Draft202012Validator.check_schema(params)
    except SchemaError as exc:
        errs.append(f"parameters: not a valid JSON Schema: {exc.message}")
        return errs
    for ref in _external_refs(params):
        errs.append(f"parameters: external $ref '{ref}' is not allowed")
    props = params.get("properties", {})
    if RESERVED_SEED in props:
        seed_type = props[RESERVED_SEED].get("type")
        if seed_type != "integer":
            errs.append("parameters: reserved parameter 'seed' must have type integer")
    errs.extend(
        f"parameters defaults: {e}"
        for e in parameter_errors(params, parameter_defaults(params))
    )

    for d in _duplicates(e["code"] for e in data.get("errors", [])):
        errs.append(f"duplicate error code '{d}'")
    if rules_1_1(data):
        for e in data.get("errors", []):
            reserved = RESERVED_ERROR_CODES.get(e["code"])
            if reserved is not None and e["category"] != reserved:
                errs.append(
                    f"error code '{e['code']}' is reserved with category '{reserved}'"
                )
    upstream = data.get("upstream")
    if upstream is not None:
        if not rules_1_2(data):
            errs.append("upstream: requires contract_version 1.2")
        elif not any(
            t["name"] == upstream["name"] and t["spdx"] == upstream["licence"]
            for t in data["licence"].get("third_party", [])
        ):
            errs.append(
                f"upstream: '{upstream['name']}' must also be listed in licence.third_party "
                f"with spdx '{upstream['licence']}'"
            )
    bundle_names = [b["name"] for b in data.get("bundles", [])]
    for d in _duplicates(bundle_names):
        errs.append(f"duplicate bundle requirement '{d}'")

    rt = data.get("reference_test")
    if rt:
        if "generator" in rt and not rules_1_1(data):
            errs.append("reference_test: 'generator' requires contract_version 1.1")
        in_names = {p["name"] for p in inputs}
        out_names = {p["name"] for p in outputs}
        for name in rt.get("inputs", {}):
            if name not in in_names:
                errs.append(f"reference_test: unknown input port '{name}'")
        for p in inputs:
            if p.get("required", True) and p["name"] not in rt.get("inputs", {}):
                errs.append(
                    f"reference_test: required input '{p['name']}' has no fixture"
                )
        for name in rt.get("expected", {}):
            if name not in out_names:
                errs.append(f"reference_test: unknown output port '{name}'")
        for name in rt.get("bundles", {}):
            if name not in bundle_names:
                errs.append(f"reference_test: unknown bundle requirement '{name}'")
        unknown = set(rt.get("parameters", {})) - set(props)
        for name in sorted(unknown):
            errs.append(f"reference_test: unknown parameter '{name}'")
        if not unknown:
            values = effective_parameters(params, rt.get("parameters", {}))
            errs.extend(
                f"reference_test parameters: {e}"
                for e in parameter_errors(params, values)
            )
    return errs


def _cond_holds(cond: dict | None, present: set[str]) -> bool:
    if not cond:
        return True
    if any(i not in present for i in cond.get("inputs_present", [])):
        return False
    if any(i in present for i in cond.get("inputs_absent", [])):
        return False
    return True


def _sources(entry: dict) -> list[str]:
    src = entry["from"]
    return src if isinstance(src, list) else [src]


NEVER, POSSIBLY, DEFINITELY = 0, 1, 2


def check_pipeline(data: dict, types: dict) -> list[str]:
    errs: list[str] = []
    inputs = {i["name"]: i for i in data.get("inputs", [])}
    for d in _duplicates(i["name"] for i in data.get("inputs", [])):
        errs.append(f"duplicate pipeline input '{d}'")
    for name, i in inputs.items():
        if i["type"] not in types:
            errs.append(f"input '{name}': unknown artefact type '{i['type']}'")
    for d in _duplicates(s["name"] for s in data.get("input_sets", [])):
        errs.append(f"duplicate input set '{d}'")
    for s in data.get("input_sets", []):
        for name in s["inputs"]:
            if name not in inputs:
                errs.append(f"input set '{s['name']}': unknown input '{name}'")
    sets = [frozenset(s["inputs"]) for s in data.get("input_sets", [])]
    for d in _duplicates(",".join(sorted(s)) for s in sets):
        errs.append(f"two input sets contain the same inputs ({d})")

    slots = {s["name"]: s for s in data.get("slots", [])}
    for d in _duplicates(s["name"] for s in data.get("slots", [])):
        errs.append(f"duplicate slot '{d}'")

    def check_cond(where: str, cond: dict | None) -> None:
        for key in ("inputs_present", "inputs_absent"):
            for name in (cond or {}).get(key, []):
                if name not in inputs:
                    errs.append(f"{where}: condition refers to unknown input '{name}'")

    deps: dict[str, set[str]] = {}
    images: dict[tuple[str, str], str] = {}
    for name, slot in slots.items():
        where = f"slot '{name}'"
        check_cond(where, slot.get("when"))
        for d in _duplicates(c["port"] for c in slot["consumes"]):
            errs.append(f"{where}: duplicate consumes port '{d}'")
        for d in _duplicates(p["port"] for p in slot["produces"]):
            errs.append(f"{where}: duplicate produces port '{d}'")
        for p in slot["produces"]:
            if p["type"] not in types:
                errs.append(
                    f"{where}: produces '{p['port']}': unknown artefact type '{p['type']}'"
                )
            if p["type"] == "run_provenance":
                errs.append(
                    f"{where}: produces '{p['port']}': run_provenance is produced by the platform only"
                )
            check_cond(f"{where} produces '{p['port']}'", p.get("when"))
        deps[name] = set()
        for c in slot["consumes"]:
            if c["type"] not in types:
                errs.append(
                    f"{where}: consumes '{c['port']}': unknown artefact type '{c['type']}'"
                )
            for src in _sources(c):
                head, _, port = src.partition(".")
                if head == "inputs":
                    if port not in inputs:
                        errs.append(
                            f"{where}: '{c['port']}' from unknown pipeline input '{port}'"
                        )
                    elif inputs[port]["type"] != c["type"]:
                        errs.append(
                            f"{where}: '{c['port']}' expects type '{c['type']}' but input '{port}' "
                            f"has type '{inputs[port]['type']}'"
                        )
                elif head == "run":
                    if c["type"] != "run_provenance":
                        errs.append(
                            f"{where}: '{c['port']}' from run.provenance must have type 'run_provenance'"
                        )
                else:
                    if head == name:
                        errs.append(f"{where}: '{c['port']}' refers to the slot itself")
                        continue
                    if head not in slots:
                        errs.append(
                            f"{where}: '{c['port']}' from unknown slot '{head}'"
                        )
                        continue
                    produced = {p["port"]: p for p in slots[head]["produces"]}
                    if port not in produced:
                        errs.append(f"{where}: '{c['port']}' from unknown port '{src}'")
                    elif produced[port]["type"] != c["type"]:
                        errs.append(
                            f"{where}: '{c['port']}' expects type '{c['type']}' but '{src}' "
                            f"has type '{produced[port]['type']}'"
                        )
                    deps[name].add(head)

        impl = slot["implementations"]
        ids = [a["id"] for a in impl["allowed"]]
        for d in _duplicates(ids):
            errs.append(f"{where}: duplicate implementation id '{d}'")
        for a in impl["allowed"]:
            key = (a["step"]["name"], a["step"]["version"])
            if images.setdefault(key, a["image"]) != a["image"]:
                errs.append(
                    f"{where}: step {key[0]} {key[1]} is pinned to two different images"
                )
        if "default" in impl and impl["default"] not in ids:
            errs.append(
                f"{where}: default implementation '{impl['default']}' is not in allowed"
            )
        for a in impl["allowed"]:
            both = set(a.get("parameters", {})) & set(a.get("parameter_bindings", {}))
            for p in sorted(both):
                errs.append(
                    f"{where}: implementation '{a['id']}' sets parameter '{p}' both literally and by binding"
                )

    if errs:
        return errs

    # Acyclicity (Kahn's algorithm).
    order: list[str] = []
    pending = {k: set(v) for k, v in deps.items()}
    while True:
        ready = sorted(k for k, v in pending.items() if not v)
        if not ready:
            break
        for k in ready:
            order.append(k)
            del pending[k]
            for v in pending.values():
                v.discard(k)
    if pending:
        return [f"slot wiring has a cycle among: {', '.join(sorted(pending))}"]

    # Static availability analysis for every input set.
    active_any: set[str] = set()
    for s in data.get("input_sets", []):
        present = set(s["inputs"])
        slot_state: dict[str, int] = {}
        port_state: dict[str, int] = {}
        for name in order:
            slot = slots[name]
            if not _cond_holds(slot.get("when"), present):
                slot_state[name] = NEVER
            else:
                worst = DEFINITELY
                for c in slot["consumes"]:
                    if c.get("optional", False):
                        continue
                    avail = max(
                        (
                            DEFINITELY
                            if (src.startswith("inputs.") and src[7:] in present)
                            or src == "run.provenance"
                            else NEVER
                            if src.startswith("inputs.")
                            else port_state.get(src, NEVER)
                        )
                        for src in _sources(c)
                    )
                    worst = min(worst, avail)
                    if avail == NEVER:
                        errs.append(
                            f"input set '{s['name']}': slot '{name}' requires '{c['port']}' "
                            f"but none of its sources can be available"
                        )
                    elif avail == POSSIBLY and not slot.get("optional", False):
                        errs.append(
                            f"input set '{s['name']}': required slot '{name}' needs '{c['port']}' "
                            f"which may be absent; make the slot optional or the source required"
                        )
                if worst == NEVER:
                    slot_state[name] = NEVER
                elif slot.get("optional", False) or worst == POSSIBLY:
                    slot_state[name] = POSSIBLY
                else:
                    slot_state[name] = DEFINITELY
            if slot_state[name] != NEVER:
                active_any.add(name)
            for p in slot["produces"]:
                key = f"{name}.{p['port']}"
                if slot_state[name] == NEVER or not _cond_holds(p.get("when"), present):
                    port_state[key] = NEVER
                elif p.get("optional", False):
                    port_state[key] = min(POSSIBLY, slot_state[name])
                else:
                    port_state[key] = slot_state[name]
    for name in slots:
        if name not in active_any:
            errs.append(f"slot '{name}' can never run under any input set")
    return errs


def bundle_digest(files: list[dict]) -> str:
    lines = "".join(
        f"{f['sha256']}  {f['path']}\n"
        for f in sorted(files, key=lambda f: f["path"].encode())
    )
    return "sha256:" + hashlib.sha256(lines.encode("utf-8")).hexdigest()


def check_bundle(data: dict) -> list[str]:
    errs = [
        f"duplicate file path '{d}'"
        for d in _duplicates(f["path"] for f in data["files"])
    ]
    expected = bundle_digest(data["files"])
    if data["content_digest"] != expected:
        errs.append(
            f"content_digest does not match the files list (expected {expected})"
        )
    return errs


def _ts_key(value: str) -> tuple[str, str]:
    main, _, frac = value.rstrip("Z").partition(".")
    return (main, frac.ljust(6, "0"))


def check_provenance(data: dict) -> list[str]:
    errs: list[str] = []
    if data["kind"] == "StepProvenance":
        if _ts_key(data["started_at"]) > _ts_key(data["finished_at"]):
            errs.append("started_at is after finished_at")
        for label in ("inputs", "outputs", "bundles"):
            key = "name" if label == "bundles" else "port"
            for d in _duplicates(r[key] for r in data[label]):
                errs.append(f"{label}: duplicate '{d}'")
        if data["status"] != "succeeded" and data["outputs"] and rules_1_1(data):
            errs.append(
                f"outputs must be empty when status is '{data['status']}' (contract 1.1)"
            )
        return errs
    slot_records = {s["slot"]: s for s in data["slots"]}
    for d in _duplicates(s["slot"] for s in data["slots"]):
        errs.append(f"slots: duplicate '{d}'")
    for i, step in enumerate(data["steps"]):
        rec = slot_records.get(step["slot"])
        if rec is None or rec["status"] != "selected":
            errs.append(f"steps/{i}: slot '{step['slot']}' is not a selected slot")
            continue
        prov = step["provenance"]
        impl = rec["implementation"]
        if prov is not None and (prov["step"] != impl["step"]):
            errs.append(
                f"steps/{i}: provenance step {prov['step']} differs from the selected implementation"
            )
        image = step["execution"].get("image")
        if image is not None and image != impl["image"]:
            errs.append(
                f"steps/{i}: executed image differs from the selected implementation image"
            )
        if step["execution"]["outcome"] == "rejected" and step["error"] is None:
            errs.append(f"steps/{i}: outcome 'rejected' requires an error record")
    if data["status"] != "running" and "finished_at" not in data:
        errs.append("finished_at is required once the run is no longer running")
    errs.extend(_run_plan_errors(data))
    return errs


def _run_1_2_fields(data: dict) -> list[str]:
    """Fields and values of a RunProvenance record that contract 1.2 introduced."""
    used = []
    if "plan" in data:
        used.append("plan")
    if any("origin" in i for i in data["inputs"]):
        used.append("inputs[].origin")
    for s in data["slots"]:
        impl = s.get("implementation", {})
        if "plan_role" in s:
            used.append(f"slot '{s['slot']}': plan_role")
        if s.get("reason") == "not_requested":
            used.append(f"slot '{s['slot']}': reason 'not_requested'")
        if impl.get("chosen_by") == "preset":
            used.append(f"slot '{s['slot']}': chosen_by 'preset'")
        if "upstream" in impl:
            used.append(f"slot '{s['slot']}': implementation.upstream")
    return used


def _run_plan_errors(data: dict) -> list[str]:
    """Rules of the 1.2 plan record (provenance.md 3, 3.1)."""
    if not rules_1_2(data):
        return [f"{f} requires contract_version 1.2" for f in _run_1_2_fields(data)]
    errs: list[str] = []
    plan = data.get("plan")
    mode = plan["mode"] if plan else None
    for s in data["slots"]:
        where = f"slot '{s['slot']}'"
        reason, role = s.get("reason"), s.get("plan_role")
        chosen_by = s.get("implementation", {}).get("chosen_by")
        if reason == "not_requested" and s["status"] != "skipped":
            errs.append(f"{where}: reason 'not_requested' requires status 'skipped'")
        if reason == "not_requested" and mode not in ("preset", "free"):
            errs.append(
                f"{where}: reason 'not_requested' requires a plan with mode 'preset' or 'free'"
            )
        if chosen_by == "preset" and mode != "preset":
            errs.append(
                f"{where}: chosen_by 'preset' requires a plan with mode 'preset'"
            )
        if role is not None and plan is None:
            errs.append(f"{where}: plan_role requires 'plan'")
        if plan is None:
            continue
        if reason in ("condition_false", "not_requested"):
            if role is not None:
                errs.append(f"{where}: a slot with reason '{reason}' has no plan_role")
        elif s["status"] == "selected":
            if role is None:
                errs.append(
                    f"{where}: a selected slot of a planned run needs plan_role"
                )
        elif role != "if_available":
            errs.append(
                f"{where}: only an 'if_available' slot may be '{s['status']}' "
                f"({reason}); a target or upstream slot without it refuses the run"
            )
    if plan is None:
        return errs
    targets = plan["targets"]
    required = targets.get("required", [])
    optional = targets.get("if_available", [])
    if (mode == "preset") != (plan["preset"] is not None):
        errs.append("plan: 'preset' is set exactly when mode is 'preset'")
    if mode == "full" and (required or optional):
        errs.append("plan: a full plan has no targets (it is defined by rule R0)")
    if mode == "preset" and not required:
        errs.append("plan: a preset plan has at least one required target")
    if mode == "free" and not (required or optional):
        errs.append("plan: a free plan has at least one target")
    for d in sorted(set(required) & set(optional)):
        errs.append(f"plan: target '{d}' is both required and if_available")
    return errs


def check_error(data: dict) -> list[str]:
    # A token starting with "/" or a drive letter looks like a file system path.
    if re.search(r"(?:^|[\s'\"(])(?:/|[A-Za-z]:\\)\S", data["message"]):
        return ["message must not contain file system paths"]
    reserved = RESERVED_ERROR_CODES.get(data["code"])
    if reserved is not None and data["category"] != reserved and rules_1_1(data):
        return [f"error code '{data['code']}' is reserved with category '{reserved}'"]
    return []


def check_artefact_types(data: dict) -> list[str]:
    errs: list[str] = []
    grids = data.get("grids", {})
    for name, t in data.get("types", {}).items():
        nifti = t.get("nifti", {})
        grid = nifti.get("grid")
        if grid is not None:
            if grid not in grids:
                errs.append(f"type '{name}': unknown grid '{grid}'")
            elif nifti.get("space") and nifti["space"] != grids[grid]["space"]:
                errs.append(
                    f"type '{name}': space '{nifti['space']}' differs from its grid's space"
                )
        space_entity = t["bids"].get("entities", {}).get("space")
        if nifti.get("space") and space_entity and space_entity != nifti["space"]:
            errs.append(f"type '{name}': BIDS space entity differs from nifti.space")
        schema = t.get("json_schema")
        if schema and not (CONTRACTS_DIR / schema).is_file():
            errs.append(f"type '{name}': json_schema '{schema}' does not exist")
    return errs


# --------------------------------------------------------------------------- run plans (contract 1.2)

# Plan roles and refusal codes of docs/contracts/presets.md (ADR-0018).
PLAN_ROLES = ("target", "upstream", "if_available")
RESOLUTION_CODES = (
    "unknown_target",
    "ambiguous_target",
    "target_not_applicable",
    "target_not_guaranteed",
)
CLOSURE_CODES = ("target_unreachable", "no_implementation")


@dataclass(frozen=True)
class PlanReason:
    """One reason why a plan request is refused (presets.md 4.3)."""

    code: str
    message: str
    target: str | None = None
    slot: str | None = None
    port: str | None = None
    required_by: str | None = None


@dataclass(frozen=True)
class SlotPlan:
    """The static plan of one slot. ``status`` is ``planned`` (an implementation is
    chosen at run creation and the slot becomes ``selected``), ``skipped`` or
    ``unavailable``; ``reason`` is the provenance reason of the last two."""

    slot: str
    status: str
    reason: str | None = None
    plan_role: str | None = None


@dataclass(frozen=True)
class StaticPlan:
    """Result of ``plan_closure``: feasible when ``reasons`` is empty."""

    input_set: str
    slots: tuple[SlotPlan, ...]  # one per pipeline slot, in pipeline order
    targets: (
        dict  # resolved request {required: [slot.port], if_available: [...]}; {} for R0
    )
    reasons: tuple[PlanReason, ...] = field(default=())

    @property
    def feasible(self) -> bool:
        return not self.reasons

    def slot(self, name: str) -> SlotPlan:
        return next(s for s in self.slots if s.slot == name)

    def planned(self) -> dict[str, str | None]:
        """Planned slots and their roles."""
        return {s.slot: s.plan_role for s in self.slots if s.status == "planned"}


def topological_order(pipeline: dict) -> list[str]:
    """Slots of a valid pipeline in dependency order (ties in pipeline order)."""
    names = [s["name"] for s in pipeline["slots"]]
    deps = {
        s["name"]: {
            src.split(".")[0]
            for c in s["consumes"]
            for src in _sources(c)
            if src.split(".")[0] not in ("inputs", "run")
        }
        for s in pipeline["slots"]
    }
    order: list[str] = []
    while len(order) < len(names):
        ready = [n for n in names if n not in order and deps[n] <= set(order)]
        if not ready:
            raise ValueError("slot wiring has a cycle")
        order.extend(ready)
    return order


def _source_state(src: str, present: set[str], port_state: dict[str, int]) -> int:
    if src == "run.provenance":
        return DEFINITELY
    if src.startswith("inputs."):
        return DEFINITELY if src[len("inputs.") :] in present else NEVER
    return port_state.get(src, NEVER)


def static_availability(
    pipeline: dict, present: set[str]
) -> tuple[dict[str, int], dict[str, int]]:
    """Per slot and per produced port (``slot.port``): NEVER, POSSIBLY or DEFINITELY
    under the run inputs ``present`` (pipeline.md 3.3)."""
    slots = {s["name"]: s for s in pipeline["slots"]}
    slot_state: dict[str, int] = {}
    port_state: dict[str, int] = {}
    for name in topological_order(pipeline):
        slot = slots[name]
        if not _cond_holds(slot.get("when"), present):
            state = NEVER
        else:
            worst = DEFINITELY
            for c in slot["consumes"]:
                if c.get("optional", False):
                    continue
                worst = min(
                    worst,
                    max(_source_state(s, present, port_state) for s in _sources(c)),
                )
            if worst == NEVER:
                state = NEVER
            elif slot.get("optional", False) or worst == POSSIBLY:
                state = POSSIBLY
            else:
                state = DEFINITELY
        slot_state[name] = state
        for p in slot["produces"]:
            key = f"{name}.{p['port']}"
            if state == NEVER or not _cond_holds(p.get("when"), present):
                port_state[key] = NEVER
            elif p.get("optional", False):
                port_state[key] = min(POSSIBLY, state)
            else:
                port_state[key] = state
    return slot_state, port_state


def _resolve_target(
    target: str,
    slots: dict[str, dict],
    present: set[str],
    slot_state: dict[str, int],
    required: bool,
    input_set: str,
) -> tuple[str, list[str]] | PlanReason:
    """Step 2 of the resolution: a target to its slot and ``slot.port`` list. An empty
    list means an if-available target that does not apply under the input set."""
    if "." in target:
        name, port = target.split(".", 1)
        produced = {p["port"]: p for p in slots.get(name, {}).get("produces", [])}
        if port not in produced:
            return PlanReason(
                "unknown_target",
                f"The target '{target}' is not a port produced by a slot of this pipeline.",
                target=target,
            )
        ports = [produced[port]]
    else:
        candidates = [
            f"{n}.{p['port']}"
            for n, s in slots.items()
            for p in s["produces"]
            if p["type"] == target
            and slot_state[n] != NEVER
            and _cond_holds(p.get("when"), present)
        ]
        if target in slots and candidates:
            return PlanReason(
                "ambiguous_target",
                f"The target '{target}' names both a slot and an artefact type produced "
                f"by {', '.join(candidates)}.",
                target=target,
            )
        if target in slots:
            name = target
            ports = [
                p
                for p in slots[name]["produces"]
                if not p.get("optional", False) and _cond_holds(p.get("when"), present)
            ]
        elif len(candidates) > 1:
            return PlanReason(
                "ambiguous_target",
                f"The artefact type '{target}' is produced by several ports under the "
                f"input set '{input_set}': {', '.join(candidates)}.",
                target=target,
            )
        elif candidates:
            name, port = candidates[0].split(".", 1)
            ports = [next(p for p in slots[name]["produces"] if p["port"] == port)]
        else:
            return PlanReason(
                "unknown_target",
                f"The target '{target}' is neither a slot nor an artefact type produced "
                f"under the input set '{input_set}'.",
                target=target,
            )
    slot = slots[name]
    if not _cond_holds(slot.get("when"), present):
        if not required:
            return name, []
        needs = ", ".join(slot["when"].get("inputs_present", []))
        return PlanReason(
            "target_not_applicable",
            f"The target '{target}' does not run with the input set '{input_set}'"
            + (f" (it needs the inputs {needs})." if needs else "."),
            target=target,
            slot=name,
        )
    if "." in target:
        p = ports[0]
        if not _cond_holds(p.get("when"), present):
            if not required:
                return name, []
            return PlanReason(
                "target_not_applicable",
                f"The port '{target}' is not produced with the input set '{input_set}'.",
                target=target,
                slot=name,
                port=p["port"],
            )
        if p.get("optional", False) and required:
            return PlanReason(
                "target_not_guaranteed",
                f"The port '{target}' is not guaranteed to be produced; it can only be "
                f"requested if available.",
                target=target,
                slot=name,
                port=p["port"],
            )
    return name, [f"{name}.{p['port']}" for p in ports] or [name]


def plan_closure(
    pipeline: dict,
    input_set: str,
    required: Iterable[str] | None = None,
    if_available: Iterable[str] | None = None,
    enabled: Mapping[str, Iterable[str]] | None = None,
) -> StaticPlan:
    """The static plan of a plan request (presets.md 4; design run-plans.md 3.2).

    ``pipeline`` is a valid pipeline document and ``input_set`` the name of one of its
    input sets. ``required`` and ``if_available`` are the targets; when both are
    ``None`` the request is the full plan (rule R0). ``enabled`` maps slot names to the
    ids of the implementations enabled in the deployment; ``None`` assumes that every
    allowed implementation is enabled (the static check of rule P5). Choosing the
    implementation, parameters and bundles is left to the orchestrator. Pure function:
    no I/O and no state. Every refusal reason found is returned, not only the first.
    """
    sets = {s["name"]: set(s["inputs"]) for s in pipeline["input_sets"]}
    if input_set not in sets:
        raise ValueError(f"unknown input set '{input_set}'")
    present = sets[input_set]
    slots = {s["name"]: s for s in pipeline["slots"]}
    names = [s["name"] for s in pipeline["slots"]]
    slot_state, port_state = static_availability(pipeline, present)
    reasons: list[PlanReason] = []

    req_slots: list[tuple[str, str]] = []  # (slot, target that names it)
    opt_slots: list[tuple[str, str]] = []
    targets: dict = {}
    if required is None and if_available is None:  # R0: the full plan
        for n in names:
            if _cond_holds(slots[n].get("when"), present):
                out = opt_slots if slots[n].get("optional", False) else req_slots
                out.append((n, n))
    else:
        targets = {"required": [], "if_available": []}
        for kind, values, out in (
            ("required", list(required or []), req_slots),
            ("if_available", list(if_available or []), opt_slots),
        ):
            for target in values:
                res = _resolve_target(
                    target, slots, present, slot_state, kind == "required", input_set
                )
                if isinstance(res, PlanReason):
                    reasons.append(res)
                    continue
                name, ports = res
                if not ports:
                    continue  # if-available target that does not apply
                out.append((name, target))
                for p in ports:
                    if "." in p and p not in targets[kind]:
                        targets[kind].append(p)
        if not targets["if_available"]:
            del targets["if_available"]

    roles: dict[str, str] = {}
    root: dict[str, str] = {}  # slot -> the target that brought it into the plan
    needed_by: dict[str, str] = {}  # slot -> the planned slot that pulled it in
    no_source: set[str] = set()  # if-available slots with a required port unreachable
    picked: dict[str, list[str]] = {}  # slot -> producer slots of its required ports

    def qualifies(src: str) -> bool:
        if src == "run.provenance" or src.startswith("inputs."):
            return _source_state(src, present, port_state) != NEVER
        head, _, port = src.partition(".")
        produced = {p["port"]: p for p in slots[head]["produces"]}
        return slot_state[head] != NEVER and _cond_holds(
            produced[port].get("when"), present
        )

    def preferred(src: str, planned: set[str]) -> bool:
        head = src.split(".")[0]
        if head in ("inputs", "run"):
            return True
        return head in planned or (
            not slots[head].get("optional", False) and slot_state[head] == DEFINITELY
        )

    def close(start: list[tuple[str, str]], role: str, prefer: tuple[str, ...]) -> None:
        queue: list[str] = []
        for name, target in start:
            if name not in roles:
                roles[name] = "target" if role == "upstream" else role
                root[name] = target
                queue.append(name)
        while queue:
            name = queue.pop(0)
            picked.setdefault(name, [])
            for c in slots[name]["consumes"]:
                if c.get("optional", False):
                    continue
                planned = {n for n, r in roles.items() if r in prefer}
                good = [s for s in _sources(c) if qualifies(s)]
                choice = next((s for s in good if preferred(s, planned)), None)
                if choice is None and good:
                    choice = good[0]
                if choice is None:
                    if role == "if_available":
                        no_source.add(name)
                        continue
                    chain = [name]
                    while chain[-1] in needed_by:
                        chain.append(needed_by[chain[-1]])
                    reasons.append(
                        PlanReason(
                            "target_unreachable",
                            f"The target '{root[name]}' cannot be reached with the input "
                            f"set '{input_set}': the slot '{name}' needs '{c['port']}' and "
                            f"none of its sources can be available (needed by: "
                            f"{' <- '.join(chain)}).",
                            target=root[name],
                            slot=name,
                            port=c["port"],
                            required_by=needed_by.get(name),
                        )
                    )
                    continue
                head = choice.split(".")[0]
                if head in ("inputs", "run"):
                    continue
                picked[name].append(head)
                if head not in roles:
                    roles[head] = role
                    root[head] = root[name]
                    needed_by[head] = name
                    queue.append(head)

    close(req_slots, "upstream", ("target", "upstream"))
    close(opt_slots, "if_available", PLAN_ROLES)

    def has_implementation(name: str) -> bool:
        allowed = {a["id"] for a in slots[name]["implementations"]["allowed"]}
        if enabled is None:
            return bool(allowed)
        return any(i in allowed for i in enabled.get(name, ()))

    result: dict[str, SlotPlan] = {}
    for name in topological_order(pipeline):
        role = roles.get(name)
        if not _cond_holds(slots[name].get("when"), present):
            result[name] = SlotPlan(name, "skipped", "condition_false")
        elif role is None:
            result[name] = SlotPlan(name, "skipped", "not_requested")
        elif role == "if_available" and (
            name in no_source
            or any(result[h].status != "planned" for h in picked.get(name, []))
        ):
            result[name] = SlotPlan(name, "skipped", "upstream_absent", role)
        elif not has_implementation(name):
            result[name] = SlotPlan(name, "unavailable", "no_implementation", role)
            if role != "if_available":
                by = needed_by.get(name)
                reasons.append(
                    PlanReason(
                        "no_implementation",
                        f"The slot '{name}' has no enabled implementation in this "
                        f"deployment"
                        + (f"; the slot '{by}' needs it" if by else "")
                        + f" for the target '{root[name]}'.",
                        target=root[name],
                        slot=name,
                        required_by=by or root[name],
                    )
                )
        else:
            result[name] = SlotPlan(name, "planned", None, role)
    return StaticPlan(
        input_set, tuple(result[n] for n in names), targets, tuple(reasons)
    )


def check_preset(data: dict) -> list[str]:
    """Single-document rules of a Preset: P1 (contract version) and P7 on the targets
    as written."""
    errs: list[str] = []
    if not rules_1_2(data):
        errs.append("a Preset requires contract_version 1.2 or later (P1)")
    targets = data["targets"]
    for d in sorted(set(targets["required"]) & set(targets.get("if_available", []))):
        errs.append(f"target '{d}' is both required and if_available (P7)")
    return errs


def preset_errors(
    preset: dict,
    pipeline: dict,
    manifests: Mapping[tuple[str, str], dict] | None = None,
) -> list[str]:
    """Rules P3-P7 of a preset against one pipeline version (presets.md 3). A fixed
    implementation is checked for ``stub`` when its manifest is in ``manifests`` (keyed
    by step name and version)."""
    errs: list[str] = []
    sets = {s["name"]: set(s["inputs"]) for s in pipeline["input_sets"]}
    slots = {s["name"]: s for s in pipeline["slots"]}
    required = preset["targets"]["required"]
    optional = preset["targets"].get("if_available", [])
    plans: dict[str, StaticPlan] = {}
    for name in preset["input_sets"]:
        if name not in sets:
            errs.append(f"input set '{name}' is not an input set of the pipeline (P3)")
            continue
        plan = plan_closure(pipeline, name, required, optional)
        plans[name] = plan
        for r in plan.reasons:
            rule = "P4" if r.code in RESOLUTION_CODES else "P5"
            errs.append(f"input set '{name}': [{r.code}] {r.message} ({rule})")
        slot_state, _ = static_availability(pipeline, sets[name])
        for target in optional:
            res = _resolve_target(target, slots, sets[name], slot_state, False, name)
            if not isinstance(res, PlanReason) and not res[1]:
                errs.append(
                    f"input set '{name}': [target_not_applicable] the if-available "
                    f"target '{target}' does not apply with this input set (P4)"
                )
        req_slots = {p.split(".")[0] for p in plan.targets.get("required", [])}
        opt_slots = {p.split(".")[0] for p in plan.targets.get("if_available", [])}
        for d in sorted(req_slots & opt_slots):
            errs.append(
                f"input set '{name}': slot '{d}' is both a required and an "
                f"if-available target (P7)"
            )
    planned = {n for plan in plans.values() for n in plan.planned()}
    for slot, choice in preset.get("implementations", {}).items():
        where = f"implementations '{slot}'"
        if slot not in slots:
            errs.append(f"{where}: not a slot of the pipeline (P6)")
            continue
        if plans and slot not in planned:
            errs.append(
                f"{where}: the slot is not in the plan for any listed input set (P6)"
            )
        allowed = {a["id"]: a for a in slots[slot]["implementations"]["allowed"]}
        impl_id = choice.get("fixed", choice.get("default"))
        if impl_id not in allowed:
            errs.append(f"{where}: '{impl_id}' is not an allowed implementation (P6)")
            continue
        if "fixed" in choice and manifests is not None:
            step = allowed[impl_id]["step"]
            manifest = manifests.get((step["name"], step["version"]))
            if manifest is not None and manifest.get("stub", False):
                errs.append(f"{where}: a stub cannot be a fixed choice (P6)")
    return errs


def preset_sha256(raw: bytes) -> str:
    """SHA-256 of the exact bytes of a preset file, as recorded in runs (P8)."""
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- cross-document rules


def slot_match_errors(slot: dict, step: dict) -> list[str]:
    """Rules M1-M5 of docs/contracts/pipeline.md."""
    errs: list[str] = []
    if step["provides"] != slot["name"]:
        errs.append(
            f"step provides '{step['provides']}', slot is '{slot['name']}' (M1)"
        )
    outs = {o["name"]: o for o in step["outputs"]}
    for p in slot["produces"]:
        o = outs.get(p["port"])
        if o is None:
            errs.append(f"step has no output '{p['port']}' (M2)")
        elif o["type"] != p["type"]:
            errs.append(
                f"output '{p['port']}' has type '{o['type']}', slot expects '{p['type']}' (M2)"
            )
        elif (
            not p.get("optional", False)
            and "when" not in p
            and not o.get("required", True)
        ):
            errs.append(
                f"output '{p['port']}' is optional in the step but guaranteed by the slot (M2)"
            )
    consumes = {c["port"]: c for c in slot["consumes"]}
    for i in step.get("inputs", []):
        c = consumes.get(i["name"])
        if i.get("required", True):
            if c is None:
                errs.append(
                    f"required input '{i['name']}' is not consumed by the slot (M3)"
                )
            elif c.get("optional", False):
                errs.append(
                    f"required input '{i['name']}' is optional in the slot (M3)"
                )
        if c is not None and c["type"] != i["type"]:
            errs.append(
                f"input '{i['name']}' has type '{i['type']}', slot provides '{c['type']}' (M4)"
            )
    major = lambda v: v.split(".")[0]  # noqa: E731
    return errs + (
        []
        if major(step["contract_version"]) == str(CONTRACT_MAJOR)
        else [
            f"step contract_version {step['contract_version']} is not {CONTRACT_MAJOR}.x (M5)"
        ]
    )


def cross_check(docs: list[Document], strict: bool) -> list[Issue]:
    issues: list[Issue] = []
    steps = {(d.data["name"], d.data["version"]): d for d in docs if d.kind == "Step"}
    bundles = {(d.data["id"], d.data["version"]): d for d in docs if d.kind == "Bundle"}
    pipelines = {
        (d.data["id"], d.data["version"]): d for d in docs if d.kind == "Pipeline"
    }

    for pdoc in pipelines.values():
        f = str(pdoc.path)
        for slot in pdoc.data["slots"]:
            for impl in slot["implementations"]["allowed"]:
                key = (impl["step"]["name"], impl["step"]["version"])
                where = f"slot '{slot['name']}' implementation '{impl['id']}'"
                sdoc = steps.get(key)
                if sdoc is None:
                    issues.append(
                        Issue(
                            f,
                            "cross",
                            f"{where}: manifest {key[0]} {key[1]} not supplied; not checked",
                            "error" if strict else "note",
                        )
                    )
                    continue
                step = sdoc.data
                for e in slot_match_errors(slot, step):
                    issues.append(Issue(f, "cross", f"{where}: {e}"))
                params = step["parameters"]
                props = params.get("properties", {})
                for name in list(impl.get("parameters", {})) + list(
                    impl.get("parameter_bindings", {})
                ):
                    if name not in props:
                        issues.append(
                            Issue(
                                f, "cross", f"{where}: step has no parameter '{name}'"
                            )
                        )
                values = effective_parameters(
                    params,
                    {k: v for k, v in impl.get("parameters", {}).items() if k in props},
                )
                for e in parameter_errors(params, values):
                    issues.append(Issue(f, "cross", f"{where}: parameters: {e}"))
                for pname, source in impl.get("parameter_bindings", {}).items():
                    if pname not in props:
                        continue
                    for sample in BINDING_SAMPLES[source]:
                        for e in parameter_errors(params, {**values, pname: sample}):
                            issues.append(
                                Issue(
                                    f,
                                    "cross",
                                    f"{where}: binding {source} -> {pname}: {e}",
                                )
                            )
                reqs = {b["name"]: b for b in step.get("bundles", [])}
                for bname, pin in impl.get("bundles", {}).items():
                    req = reqs.get(bname)
                    if req is None:
                        issues.append(
                            Issue(
                                f,
                                "cross",
                                f"{where}: step has no bundle requirement '{bname}'",
                            )
                        )
                        continue
                    if "id" in req and req["id"] != pin["id"]:
                        issues.append(
                            Issue(
                                f,
                                "cross",
                                f"{where}: bundle '{bname}' must have id '{req['id']}'",
                            )
                        )
                    if "version" in req and not satisfies(
                        pin["version"], req["version"]
                    ):
                        issues.append(
                            Issue(
                                f,
                                "cross",
                                f"{where}: bundle '{bname}' {pin['version']} not in '{req['version']}'",
                            )
                        )
                    bdoc = bundles.get((pin["id"], pin["version"]))
                    if bdoc is not None and bdoc.data["type"] != req["type"]:
                        issues.append(
                            Issue(
                                f,
                                "cross",
                                f"{where}: bundle '{pin['id']}' has type '{bdoc.data['type']}', "
                                f"step requires '{req['type']}'",
                            )
                        )

    for d in docs:
        if d.kind == "StepError":
            sdoc = steps.get((d.data["step"]["name"], d.data["step"]["version"]))
            if sdoc is not None:
                for e in _error_vs_step(d.data, sdoc.data):
                    issues.append(Issue(str(d.path), "cross", e))
        if d.kind == "StepProvenance":
            sdoc = steps.get((d.data["step"]["name"], d.data["step"]["version"]))
            if sdoc is not None:
                for e in _provenance_vs_step(d.data, sdoc.data):
                    issues.append(Issue(str(d.path), "cross", e))
        if d.kind == "RunProvenance":
            p = d.data["pipeline"]
            pdoc = pipelines.get((p["id"], p["version"]))
            if pdoc is not None:
                for e in _run_vs_pipeline(d.data, pdoc.data):
                    issues.append(Issue(str(d.path), "cross", e))
            for i, step in enumerate(d.data["steps"]):
                prov = step["provenance"]
                if prov is None:
                    continue
                sdoc = steps.get((prov["step"]["name"], prov["step"]["version"]))
                if sdoc is not None:
                    for e in _provenance_vs_step(prov, sdoc.data):
                        issues.append(Issue(str(d.path), "cross", f"steps/{i}: {e}"))
            for e in _run_upstream_vs_steps(d.data, steps):
                issues.append(Issue(str(d.path), "cross", e))
    issues.extend(_cross_check_presets(docs, steps, strict))
    return issues


def _run_upstream_vs_steps(run: dict, steps: dict) -> list[str]:
    """1.2: a slot record's implementation.upstream is the manifest's upstream block."""
    errs = []
    for rec in run["slots"]:
        impl = rec.get("implementation")
        if impl is None:
            continue
        sdoc = steps.get((impl["step"]["name"], impl["step"]["version"]))
        if sdoc is None:
            continue
        expected = sdoc.data.get("upstream")
        if "upstream" in impl and impl["upstream"] != expected:
            errs.append(
                f"slot '{rec['slot']}': implementation.upstream differs from the step manifest"
            )
        elif "upstream" not in impl and expected is not None and rules_1_2(run):
            errs.append(
                f"slot '{rec['slot']}': the step manifest has an upstream block, which the "
                f"slot record must copy"
            )
    return errs


def _cross_check_presets(
    docs: list[Document], steps: dict, strict: bool
) -> list[Issue]:
    """P2 (attachment to every supplied pipeline version in range), P3-P7 per version,
    and P8 (one document per pipeline id, preset id and version)."""
    issues: list[Issue] = []
    manifests = {key: d.data for key, d in steps.items()}
    seen: dict[tuple[str, str, str], Document] = {}
    for d in docs:
        if d.kind != "Preset":
            continue
        f, preset = str(d.path), d.data
        key = (preset["pipeline"]["id"], preset["id"], preset["version"])
        first = seen.setdefault(key, d)
        if first is not d and first.path != d.path:
            same = (
                first.raw is not None
                and d.raw is not None
                and preset_sha256(first.raw) == preset_sha256(d.raw)
            )
            issues.append(
                Issue(
                    f,
                    "cross",
                    f"preset {key[1]} {key[2]} of pipeline {key[0]} is also defined in "
                    f"{first.path}"
                    + ("" if same else " with different content")
                    + "; a preset version identifies one file (P8)",
                )
            )
        candidates = [
            p for p in docs if p.kind == "Pipeline" and p.data["id"] == key[0]
        ]
        attached = [
            p
            for p in candidates
            if satisfies(p.data["version"], preset["pipeline"]["versions"])
        ]
        if not attached:
            issues.append(
                Issue(
                    f,
                    "cross",
                    f"no supplied version of pipeline {key[0]} is in "
                    f"'{preset['pipeline']['versions']}'; P2-P7 not checked",
                    "error" if strict else "note",
                )
            )
        for p in attached:
            where = f"pipeline {key[0]} {p.data['version']}"
            for e in preset_errors(preset, p.data, manifests):
                issues.append(Issue(f, "cross", f"{where}: {e}"))
            slots = {s["name"]: s for s in p.data["slots"]}
            for slot, choice in preset.get("implementations", {}).items():
                if "fixed" not in choice or slot not in slots:
                    continue
                allowed = {
                    a["id"]: a for a in slots[slot]["implementations"]["allowed"]
                }
                impl = allowed.get(choice["fixed"])
                if impl is None:
                    continue
                skey = (impl["step"]["name"], impl["step"]["version"])
                if skey not in manifests:
                    issues.append(
                        Issue(
                            f,
                            "cross",
                            f"{where}: fixed implementation '{choice['fixed']}': manifest "
                            f"{skey[0]} {skey[1]} not supplied; stub not checked (P6)",
                            "error" if strict else "note",
                        )
                    )
    return issues


def _provenance_vs_step(prov: dict, step: dict) -> list[str]:
    errs = []
    if prov["contract_version"] != step["contract_version"]:
        errs.append("provenance contract_version differs from the step manifest")
    for label, ports in (
        ("inputs", step.get("inputs", [])),
        ("outputs", step["outputs"]),
    ):
        types = {p["name"]: p["type"] for p in ports}
        for r in prov[label]:
            if r["port"] not in types:
                errs.append(f"{label}: port '{r['port']}' is not declared by the step")
            elif types[r["port"]] != r["type"]:
                errs.append(
                    f"{label}: port '{r['port']}' has type '{r['type']}', step declares '{types[r['port']]}'"
                )
    if prov["status"] == "succeeded":
        produced = {r["port"] for r in prov["outputs"]}
        for o in step["outputs"]:
            if o.get("required", True) and o["name"] not in produced:
                errs.append(
                    f"outputs: required output '{o['name']}' missing from a succeeded run"
                )
    # Only a succeeded record holds validated effective parameters; a rejected or failed
    # record may hold the raw supplied values (provenance.md section 2).
    if prov["status"] == "succeeded":
        errs.extend(
            f"parameters: {e}"
            for e in parameter_errors(step["parameters"], prov["parameters"])
        )
    names = {b["name"] for b in step.get("bundles", [])}
    errs.extend(
        f"bundles: '{b['name']}' is not required by the step"
        for b in prov["bundles"]
        if b["name"] not in names
    )
    return errs


def _error_vs_step(error: dict, step: dict) -> list[str]:
    """step-contract.md 4.2-4.4: an exit-10 code must be declared or reserved."""
    errs = []
    if error["contract_version"] != step["contract_version"]:
        errs.append("error contract_version differs from the step manifest")
    if error["category"] in REJECTION_CATEGORIES:
        category = declared_error_codes(step).get(error["code"])
        if category is None:
            errs.append(f"error code '{error['code']}' is not declared by the step")
        elif category != error["category"]:
            errs.append(
                f"error code '{error['code']}' is declared with category '{category}', "
                f"not '{error['category']}'"
            )
    port = error.get("port")
    if port is not None and port not in {p["name"] for p in step.get("inputs", [])}:
        errs.append(f"port '{port}' is not an input port of the step")
    return errs


def _run_vs_pipeline(run: dict, pipeline: dict) -> list[str]:
    errs = []
    if run["input_set"] not in {s["name"] for s in pipeline["input_sets"]}:
        errs.append(f"input_set '{run['input_set']}' is not defined by the pipeline")
    slots = {s["name"]: s for s in pipeline["slots"]}
    if [s["slot"] for s in run["slots"]] != [s["name"] for s in pipeline["slots"]]:
        errs.append("slots must list every pipeline slot once, in pipeline order")
    for rec in run["slots"]:
        slot = slots.get(rec["slot"])
        if slot is None or "implementation" not in rec:
            continue
        allowed = {a["id"]: a for a in slot["implementations"]["allowed"]}
        a = allowed.get(rec["implementation"]["id"])
        if a is None:
            errs.append(
                f"slot '{rec['slot']}': implementation '{rec['implementation']['id']}' is not allowed"
            )
        elif (
            a["image"] != rec["implementation"]["image"]
            or a["step"] != rec["implementation"]["step"]
        ):
            errs.append(
                f"slot '{rec['slot']}': implementation image or step differs from the pipeline"
            )
    plan = run.get("plan")
    if plan is not None:
        roles = {rec["slot"]: rec.get("plan_role") for rec in run["slots"]}
        for kind in ("required", "if_available"):
            for target in plan["targets"].get(kind, []):
                name, _, port = target.partition(".")
                produced = {p["port"] for p in slots.get(name, {}).get("produces", [])}
                if port not in produced:
                    errs.append(
                        f"plan: target '{target}' is not a port produced by the pipeline"
                    )
                elif kind == "required" and roles.get(name) != "target":
                    errs.append(
                        f"plan: the slot of the required target '{target}' must have "
                        f"plan_role 'target'"
                    )
    return errs


# --------------------------------------------------------------------------- driver


def load_types(path: Path) -> tuple[dict, list[Issue]]:
    data = load_file(path)
    errs = schema_errors(data, "artefact-types")
    issues = [Issue(str(path), "schema", e) for e in errs]
    if not errs:
        issues += [Issue(str(path), "semantic", e) for e in check_artefact_types(data)]
    return (data.get("types", {}) if isinstance(data, dict) else {}), issues


SEMANTIC = {
    "step": lambda d, t: check_step(d, t),
    "pipeline": lambda d, t: check_pipeline(d, t),
    "bundle": lambda d, t: check_bundle(d),
    "provenance": lambda d, t: check_provenance(d),
    "error": lambda d, t: check_error(d),
    "artefact-types": lambda d, t: check_artefact_types(d),
    "preset": lambda d, t: check_preset(d),
}


def validate_files(
    paths: list[Path],
    kind: str | None = None,
    types_path: Path = DEFAULT_TYPES_PATH,
    strict: bool = False,
) -> list[Issue]:
    types, issues = load_types(types_path)
    valid_docs: list[Document] = []
    for path in paths:
        f = str(path)
        try:
            raw = path.read_bytes()
            data = load_file(path)
        except Exception as exc:  # noqa: BLE001 - report any parse failure
            issues.append(Issue(f, "load", f"cannot parse: {exc}"))
            continue
        schema = detect_schema(data, path, kind)
        if schema is None:
            issues.append(
                Issue(f, "load", "cannot detect the document kind; use --kind")
            )
            continue
        doc = Document(path, data, schema, raw)
        errs = schema_errors(data, schema)
        if errs:
            issues.extend(Issue(f, "schema", e) for e in errs)
            continue
        sem = check_contract_version(doc) + SEMANTIC[schema](data, types)
        if sem:
            issues.extend(Issue(f, "semantic", e) for e in sem)
            continue
        valid_docs.append(doc)
    issues.extend(cross_check(valid_docs, strict))
    return issues


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"Validate step-contract documents (contract {CONTRACT_VERSION})."
    )
    parser.add_argument("files", nargs="+", type=Path, help="YAML or JSON documents")
    parser.add_argument(
        "--kind", choices=sorted(SCHEMA_FILES), help="force the schema for every file"
    )
    parser.add_argument(
        "--types", type=Path, default=DEFAULT_TYPES_PATH, help="artefact type registry"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat unchecked cross references as errors",
    )
    args = parser.parse_args(argv)

    for p in args.files:
        if not p.is_file():
            parser.error(f"not a file: {p}")
    issues = validate_files(args.files, args.kind, args.types, args.strict)
    failed = {i.file for i in issues if i.severity == "error"}
    for issue in issues:
        print(issue)
    for p in args.files:
        if str(p) not in failed:
            print(f"OK {p}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
