#!/usr/bin/env python3
"""Registry tool of the stroke-lnm pipeline (card F0-33).

    registry.py check [--dir DIR]
        pipeline.yaml and registry.yaml agree (slots, implementation ids, steps, images);
        profiles only enable existing, non-planned implementations; stubs are enabled only
        in the ci profile; defaults are enabled; no profile other than ci enables an
        implementation below the validation level `reference-tested`. With `published: true`
        (set by `pin`) every implementation the `deployment` profile enables, except
        `build: local` ones, must reference a real digest, not the placeholder.
        An entry with `origin: external` (card F1-23, run-plans.md 4.2 to 4.4) must carry a
        `licence_review`, a `validation` status and, in the step manifest, contract 1.2 with an
        `upstream` block whose image is pinned by digest in the Dockerfile; `build: local`
        goes with an image the licence forbids redistributing. The manifest is read from this
        repository (sources of this repository) or from the fetched sources (`--external DIR`). The presets in DIR/presets validate against
        this pipeline version (contract rules P1 and P3-P8, contracts/validate.py), are named
        after their file, and `profiles.<p>.presets` enables only presets that exist (with the
        default among the enabled ones). The optional `title` of an entry and the `in_development`
        list (display only: stages the UI shows greyed and never selectable) are validated too.
        A wrapper of this repository that consumes an author's repository unmodified (card
        STEPS-01) names it in `source.upstream: {repository, commit}`, which must equal the
        step's `upstream.env` (`UPSTREAM_REPOSITORY`, `UPSTREAM_COMMIT`); a step directory with
        an `upstream.env` needs the field.

    registry.py materialize --digests FILE --out DIR [--dir DIR]
        Write DIR/stroke-lnm/pipeline.yaml and DIR/stroke-lnm/registry.yaml with the image
        of every implementation replaced by the reference in FILE (a JSON object
        {"<step name>@<version>": "<repository>@sha256:<digest>"}). Planned
        implementations keep their placeholder. CI builds the steps and writes the digests
        of the local images; a deployment uses the digests of the published images. The preset
        files are copied byte for byte (runs record their SHA-256).

    registry.py pin --digests FILE [--dir DIR]
        Write the published image digests of FILE (the ``--digests-out`` file of
        ``release.py run``, same format as for ``materialize``) into DIR/registry.yaml and
        DIR/pipeline.yaml in place, replacing the image reference of every entry named in
        FILE (comments and layout are kept). A reference must be digest-pinned, not a
        placeholder and in the repository the entry already names; ``build: local`` entries
        and unknown steps are refused. When every implementation of the ``deployment`` profile
        (except ``build: local`` ones) has a real digest afterwards, ``published: true`` is set.
        The result is checked; on any error the files are left unchanged.

    registry.py fetch --dest DIR
        Fetch the external step sources at the commits pinned in registry.yaml, and the
        repositories that wrappers of this repository consume (`source.upstream`), into
        DIR/<repository name> (CI, release).

    registry.py build --external DIR --prefix HOST/PATH --digests-out FILE --manifests-out DIR
        Build every non-planned step image from this repository or from the fetched
        sources, push it to the (throwaway) registry at the prefix, write the pushed
        references to FILE and the manifest read from each image (/opt/step/step.yaml,
        step-contract.md 1.1) to DIR/<name>-<version>/step.yaml.

``enabled_settings(registry, profile)`` gives the rows for ``slot_implementation_settings``
of a profile (the admin endpoints of F0-30 edit the same table in a running deployment) and
``enabled_preset_settings(registry, profile, preset_ids)`` those for ``preset_settings``
(card F1-20; admin endpoints of F1-21).
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # contracts/validate.py (the preset rules)
DIGEST_REF = re.compile(
    r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+@sha256:[a-f0-9]{64}$"
)
PLACEHOLDER = "@sha256:" + "0" * 64
REPO_ROOT = HERE.parents[1]

# Registry fields of card F1-23. The level rule is the same as backend/app/core/validation.py
# (a test keeps them identical).
VALIDATION_LEVELS = ("unvalidated", "reference-tested", "validated")
ORIGINS = ("internal", "external")
BUILDS = ("published", "local")
REDISTRIBUTION = ("allowed", "forbidden")
LICENCE_REVIEW_KEYS = ("code", "weights", "image_redistribution", "notes")
# status: reviewed (default) needs `reviewed` (date) and `reviewed_by`; pending (not yet
# confirmed by the responsible person) needs neither and must not set them.
LICENCE_REVIEW_STATUSES = ("reviewed", "pending")
LICENCE_REVIEW_OPTIONAL = ("status", "reviewed", "reviewed_by")
# Registries whose images are public: a `build: local` entry never points at one.
PUBLIC_REGISTRY_HOSTS = ("docker.io", "index.docker.io", "registry-1.docker.io", "ghcr.io", "quay.io")
# Lowest level a profile may enable (run-plans.md 4.4): only the ci profile takes unvalidated.
PROFILE_MIN_VALIDATION = {"ci": "unvalidated"}
DEFAULT_PROFILE_MIN_VALIDATION = "reference-tested"


def validation_level(entry: dict) -> str:
    """Validation level of a registry entry (run-plans.md 4.4): its `validation.status`;
    `unvalidated` when none is declared (the check requires one for every real step)."""
    declared = (entry.get("validation") or {}).get("status")
    return declared if declared in VALIDATION_LEVELS else "unvalidated"


def level_rank(level: str) -> int:
    return VALIDATION_LEVELS.index(level)


def _version_tuple(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", str(text))[:3])


def _from_images(dockerfile: str) -> list[str] | None:
    """Base image references of the FROM lines (stage aliases and scratch excluded); None if
    a reference is built from a variable and so cannot be checked."""
    aliases: set[str] = set()
    images: list[str] = []
    for line in dockerfile.splitlines():
        m = re.match(r"\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?\s*$", line, re.I)
        if not m:
            continue
        ref, alias = m.group(1), m.group(2)
        is_stage = ref.lower() in aliases
        if alias:
            aliases.add(alias.lower())
        if ref.lower() == "scratch" or is_stage:
            continue
        if "$" in ref:
            return None
        images.append(ref)
    return images


def _manifest_path(entry: dict, root: Path, external: Path | None) -> Path | None:
    source = entry["source"]
    stem = Path(source["dockerfile"]).parent
    if source.get("repository") == "this":
        return root / stem / "step.yaml"
    if external is None:
        return None
    return _external_dir(external, source["repository"]) / stem / "step.yaml"


def external_errors(
    name: str,
    impl_id: str,
    entry: dict,
    *,
    root: Path = REPO_ROOT,
    external: Path | None = None,
) -> list[str]:
    """Rules of the F1-23 fields (run-plans.md 4.2 to 4.4): `origin`, `build`, `validation` and
    `licence_review` of any entry, then, for `origin: external`, what the manifest and the
    Dockerfile must show."""
    where = f"slot {name}, {impl_id}"
    errors: list[str] = []
    origin = entry.get("origin", "internal")
    external_origin = origin == "external"
    if origin not in ORIGINS:
        errors.append(f"{where}: unknown origin {origin!r}")
    build = entry.get("build", "published")
    if build not in BUILDS:
        errors.append(f"{where}: unknown build {build!r}")
    if build == "local" and not external_origin:
        errors.append(f"{where}: build: local is for origin: external entries")
    validation = entry.get("validation")
    if validation is None and entry["kind"] == "real":
        errors.append(
            f"{where}: a real implementation needs an explicit validation.status "
            "(nothing is validated implicitly)"
        )
    if validation is not None:
        status = validation.get("status") if isinstance(validation, dict) else None
        if status not in VALIDATION_LEVELS:
            errors.append(f"{where}: validation.status must be one of {list(VALIDATION_LEVELS)}")
        elif entry["kind"] == "stub" and status != "unvalidated":
            errors.append(f"{where}: a stub is unvalidated")
        elif status == "validated":
            record = validation.get("record")
            if not record or not (root / str(record)).is_file():
                errors.append(
                    f"{where}: a validated implementation needs validation.record, "
                    "a file of the repository"
                )
            if not str(validation.get("reviewed_by", "")).strip():
                errors.append(
                    f"{where}: a validated implementation needs validation.reviewed_by "
                    "(the responsible scientist, not the wrapper author)"
                )
    review = entry.get("licence_review")
    if review is not None or external_origin:
        if not isinstance(review, dict):
            errors.append(f"{where}: an external implementation needs a licence_review")
            review = {}
        for key in LICENCE_REVIEW_KEYS:
            if not str(review.get(key, "")).strip():
                errors.append(f"{where}: licence_review.{key} is missing")
        extra = set(review) - set(LICENCE_REVIEW_KEYS) - set(LICENCE_REVIEW_OPTIONAL)
        if extra:
            errors.append(f"{where}: unknown licence_review keys {sorted(extra)}")
        review_status = review.get("status", "reviewed")
        if review_status not in LICENCE_REVIEW_STATUSES:
            errors.append(
                f"{where}: licence_review.status must be one of {list(LICENCE_REVIEW_STATUSES)}"
            )
        elif review_status == "reviewed":
            for key in ("reviewed", "reviewed_by"):
                if not str(review.get(key, "")).strip():
                    errors.append(f"{where}: licence_review.{key} is missing")
        else:
            for key in ("reviewed", "reviewed_by"):
                if str(review.get(key, "")).strip():
                    errors.append(f"{where}: a pending licence_review has no {key}")
        if validation is not None and validation.get("status") == "validated" and review_status != "reviewed":
            errors.append(f"{where}: a validated implementation needs a reviewed licence_review")
        redistribution = review.get("image_redistribution")
        if redistribution and redistribution not in REDISTRIBUTION:
            errors.append(
                f"{where}: licence_review.image_redistribution must be one of {list(REDISTRIBUTION)}"
            )
        reviewed = str(review.get("reviewed", "")).strip()
        if reviewed:
            try:
                date.fromisoformat(reviewed)
            except ValueError:
                errors.append(f"{where}: licence_review.reviewed must be a YYYY-MM-DD date")
        if redistribution == "forbidden" and build != "local":
            errors.append(
                f"{where}: the licence forbids publishing the image, so the entry must be build: local"
            )
        if build == "local" and redistribution != "forbidden":
            errors.append(
                f"{where}: build: local is only for licence_review.image_redistribution: forbidden"
            )
    if build == "local":
        host = str(entry.get("image", "")).split("/", 1)[0]
        if host in PUBLIC_REGISTRY_HOSTS:
            errors.append(
                f"{where}: a build: local image is never on a public registry; keep the "
                "placeholder (or the deployment's own registry reference)"
            )
    if level_rank(validation_level(entry)) >= 1 and entry["kind"] == "real":
        own = _manifest_path(entry, root, None)
        if own is not None and own.is_file():
            try:
                own_manifest = yaml.safe_load(own.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                own_manifest = {}
            if "reference_test" not in own_manifest:
                errors.append(
                    f"{where}: reference-tested and validated need a reference_test in the manifest"
                )
    if not external_origin:
        return errors
    if validation is None:
        errors.append(f"{where}: an external implementation needs a validation status")
    if entry["kind"] == "stub":
        errors.append(f"{where}: a stub cannot be external")
    if entry["kind"] != "real":
        return errors  # planned: no manifest to read yet
    path = _manifest_path(entry, root, external)
    if path is None:
        errors.append(
            f"{where}: the manifest of {entry['source']['repository']} is not available; "
            "pass --external with the fetched sources (registry.py fetch)"
        )
        return errors
    if not path.is_file():
        errors.append(f"{where}: manifest {path} not found")
        return errors
    try:
        manifest = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return errors + [f"{where}: manifest {path} does not parse: {exc}"]
    if (manifest.get("name"), manifest.get("version")) != (
        entry["step"]["name"],
        entry["step"]["version"],
    ):
        errors.append(
            f"{where}: the manifest declares {manifest.get('name')} {manifest.get('version')}, "
            "not the registry's step"
        )
    if _version_tuple(manifest.get("contract_version", "0")) < (1, 2):
        errors.append(f"{where}: an external step declares contract 1.2 or later (upstream block)")
    upstream = manifest.get("upstream")
    if not isinstance(upstream, dict):
        errors.append(f"{where}: the manifest has no upstream block")
        return errors
    if not str(upstream.get("citation", "")).strip():
        errors.append(f"{where}: an external implementation needs upstream.citation")
    if not DIGEST_REF.match(str(upstream.get("image", ""))):
        errors.append(f"{where}: upstream.image is not pinned by digest")
    third = {t.get("name"): t.get("spdx") for t in manifest.get("licence", {}).get("third_party", [])}
    if third.get(upstream.get("name")) != upstream.get("licence"):
        errors.append(
            f"{where}: the upstream tool is not listed in licence.third_party with the same licence"
        )
    if str(review.get("code", "")).strip() and review["code"] != upstream.get("licence"):
        errors.append(f"{where}: licence_review.code differs from upstream.licence")
    dockerfile = path.parent / Path(entry["source"]["dockerfile"]).name
    if not dockerfile.is_file():
        errors.append(f"{where}: {dockerfile} not found")
        return errors
    images = _from_images(dockerfile.read_text(encoding="utf-8"))
    if images is None:
        errors.append(
            f"{where}: a FROM line of the Dockerfile uses a variable; the upstream digest cannot be checked"
        )
        return errors
    unpinned = [i for i in images if not re.search(r"@sha256:[a-f0-9]{64}$", i)]
    if unpinned or not images:
        errors.append(
            f"{where}: every base image of the Dockerfile is pinned by digest (not {unpinned or 'none'})"
        )
    digest = str(upstream.get("image", "")).rsplit("@", 1)[-1]
    if not any(i.endswith("@" + digest) for i in images):
        errors.append(f"{where}: upstream.image digest is not the base image of the Dockerfile")
    return errors


UPSTREAM_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
UPSTREAM_ENV = "upstream.env"


def read_upstream_env(path: Path) -> dict[str, str]:
    """``KEY=VALUE`` lines of a step's upstream.env (comments and blank lines ignored; one pair
    of surrounding double quotes is removed, as the shell does)."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] == '"':
                value = value[1:-1]
            values[key.strip()] = value
    return values


def upstream_errors(name: str, impl_id: str, entry: dict, *, root: Path = REPO_ROOT) -> list[str]:
    """`source.upstream` (card STEPS-01): a wrapper of this repository that consumes an
    author's repository unmodified at a pinned commit. The step directory's upstream.env is
    what its Dockerfile and CI read; the registry repeats it and the two must agree."""
    where = f"slot {name}, {impl_id}"
    source = entry.get("source") or {}
    upstream = source.get("upstream")
    errors: list[str] = []
    env_path = None
    if source.get("repository") == "this" and source.get("dockerfile"):
        env_path = root / Path(source["dockerfile"]).parent / UPSTREAM_ENV
    if upstream is None:
        if env_path is not None and env_path.is_file():
            errors.append(
                f"{where}: {env_path.relative_to(root).as_posix()} exists, so the entry needs "
                "source.upstream {repository, commit}"
            )
        return errors
    if not isinstance(upstream, dict) or set(upstream) != {"repository", "commit"}:
        return [f"{where}: source.upstream must be {{repository, commit}}"]
    if source.get("repository") != "this":
        errors.append(f"{where}: source.upstream is for wrappers of this repository (repository: this)")
    if not UPSTREAM_REPOSITORY.match(str(upstream["repository"])):
        errors.append(f"{where}: source.upstream.repository must be <owner>/<name>")
    if not COMMIT.match(str(upstream["commit"])):
        errors.append(f"{where}: source.upstream.commit must be a 40-hex commit")
    if env_path is not None and not env_path.parent.is_dir():
        return errors  # the step directory is not under this root (a partial test tree)
    if env_path is None or not env_path.is_file():
        errors.append(f"{where}: source.upstream needs {UPSTREAM_ENV} next to the step's Dockerfile")
        return errors
    env = read_upstream_env(env_path)
    rel = env_path.relative_to(root).as_posix()
    if not re.fullmatch(r"[0-9a-f]{64}", env.get("UPSTREAM_SHA256", "")):
        errors.append(f"{where}: {rel} needs UPSTREAM_SHA256 (64 hex, steps/upstream-digest.sh)")
    if not env.get("UPSTREAM_PATHS", "").split():
        errors.append(f"{where}: {rel} needs UPSTREAM_PATHS (the files the wrapper uses)")
    if (env.get("UPSTREAM_REPOSITORY"), env.get("UPSTREAM_COMMIT")) != (
        upstream["repository"],
        upstream["commit"],
    ):
        errors.append(
            f"{where}: source.upstream differs from {env_path.relative_to(root).as_posix()} "
            f"({env.get('UPSTREAM_REPOSITORY')} {env.get('UPSTREAM_COMMIT')})"
        )
    return errors


def published_errors(registry: dict) -> list[str]:
    """With ``published: true`` the ``deployment`` profile must reference real digests: a
    deployment pulls by digest and never a placeholder (docs/deployment.md, "Publishing
    step images"). ``build: local`` entries are built by each deployment and are exempt."""
    published = registry.get("published", False)
    if not isinstance(published, bool):
        return ["published must be true or false"]
    if not published:
        return []
    errors: list[str] = []
    enabled = registry["profiles"].get("deployment", {}).get("enabled", {})
    for name, ids in enabled.items():
        for impl_id in ids:
            entry = registry["implementations"].get(name, {}).get(impl_id)
            if entry is None or entry["kind"] == "planned" or entry.get("build") == "local":
                continue
            if PLACEHOLDER in str(entry["image"]) or not DIGEST_REF.match(str(entry["image"])):
                errors.append(
                    f"published: true, but {name}/{impl_id} has no real image digest "
                    "(run release.py, then registry.py pin)"
                )
    return errors


KEBAB = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


def display_errors(registry: dict, slots: set[str]) -> list[str]:
    """Display-only fields: the optional `title` of an implementation entry and the top-level
    `in_development` list (stages shown greyed and never selectable, docs/processing-pipeline.md).
    An entry with `slot` names an existing slot; one without needs a `title` and must not reuse a
    slot name. Nothing here makes anything enabled: these are not implementations."""
    errors: list[str] = []
    for name, impls in registry["implementations"].items():
        for impl_id, entry in impls.items():
            if "title" in entry and not str(entry["title"]).strip():
                errors.append(f"slot {name}, {impl_id}: title must be a non-empty text")
    stages = registry.get("in_development", [])
    if not isinstance(stages, list):
        return errors + ["in_development must be a list"]
    seen: set[str] = set()
    for index, stage in enumerate(stages):
        where = f"in_development[{index}]"
        if not isinstance(stage, dict):
            errors.append(f"{where}: must be a mapping")
            continue
        extra = set(stage) - {"id", "slot", "title", "description"}
        if extra:
            errors.append(f"{where}: unknown keys {sorted(extra)}")
        stage_id = str(stage.get("id", ""))
        if not KEBAB.match(stage_id):
            errors.append(f"{where}: id must be kebab-case")
        if stage_id in seen:
            errors.append(f"{where}: id {stage_id!r} is listed twice")
        seen.add(stage_id)
        slot = stage.get("slot")
        if slot is not None:
            if slot not in slots:
                errors.append(f"{where}: slot {slot!r} is not a slot of the pipeline")
        else:
            if not str(stage.get("title", "")).strip():
                errors.append(f"{where}: a stage without a slot needs a title")
            if stage_id in slots:
                errors.append(f"{where}: id {stage_id!r} is a slot name; give it `slot:`")
    return errors


def load(directory: Path = HERE) -> tuple[dict, dict]:
    pipeline = yaml.safe_load((directory / "pipeline.yaml").read_text(encoding="utf-8"))
    registry = yaml.safe_load((directory / "registry.yaml").read_text(encoding="utf-8"))
    return pipeline, registry


def load_presets(directory: Path = HERE) -> dict[str, tuple[dict, bytes]]:
    """``{file stem: (document, raw bytes)}`` of ``DIR/presets/*.yaml`` (an unparsable file
    gives an empty document, which ``check`` reports)."""
    out: dict[str, tuple[dict, bytes]] = {}
    for path in sorted((directory / "presets").glob("*.yaml")):
        raw = path.read_bytes()
        try:
            data = yaml.safe_load(raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError):
            data = None
        out[path.stem] = (data if isinstance(data, dict) else {}, raw)
    return out


def preset_check(
    pipeline: dict, registry: dict, presets: dict[str, tuple[dict, bytes]]
) -> list[str]:
    """Presets against this pipeline version (contract rules P1, P3-P7; P8 by file name)
    and the profiles' ``presets`` entries."""
    from contracts import validate as contract

    errors: list[str] = []
    for stem, (data, raw) in presets.items():
        where = f"preset {stem}"
        if not data:
            errors.append(f"{where}: not a YAML mapping")
            continue
        found = contract.schema_errors(data, "preset")
        if found:
            errors += [f"{where}: {e}" for e in found]
            continue
        doc = contract.Document(Path(f"{stem}.yaml"), data, "preset", raw)
        found = contract.check_contract_version(doc) + contract.check_preset(data)
        errors += [f"{where}: {e}" for e in found]
        if data["id"] != stem:
            errors.append(f"{where}: the id {data['id']!r} must equal the file name (P8)")
        if data["pipeline"]["id"] != pipeline["id"]:
            errors.append(f"{where}: written for the pipeline {data['pipeline']['id']!r}")
            continue
        if not contract.satisfies(pipeline["version"], data["pipeline"]["versions"]):
            errors.append(
                f"{where}: range {data['pipeline']['versions']!r} does not include "
                f"pipeline version {pipeline['version']} (P2), so it would not be offered"
            )
            continue
        errors += [f"{where}: {e}" for e in contract.preset_errors(data, pipeline)]
    for pname, profile in registry["profiles"].items():
        spec = profile.get("presets")
        if spec is None:
            continue
        enabled = spec.get("enabled", [])
        for preset_id in enabled:
            if preset_id not in presets:
                errors.append(f"profile {pname}: enabled preset {preset_id!r} has no file")
        if len(set(enabled)) != len(enabled):
            errors.append(f"profile {pname}: a preset is listed twice")
        default = spec.get("default")
        if default is not None and default not in enabled:
            errors.append(f"profile {pname}: default preset {default!r} is not enabled")
    return errors


def check(
    pipeline: dict,
    registry: dict,
    presets: dict[str, tuple[dict, bytes]] | None = None,
    *,
    root: Path = REPO_ROOT,
    external: Path | None = None,
) -> list[str]:
    """Problems of the two documents; with ``presets`` (``load_presets``) also those of
    the presets and of the profiles' ``presets`` entries. ``root`` is the repository (step
    manifests and Dockerfiles of ``repository: this`` sources), ``external`` the directory
    of the fetched external sources."""
    errors: list[str] = []
    ident = {"id": pipeline["id"], "version": pipeline["version"]}
    if registry.get("pipeline") != ident:
        errors.append("registry.yaml names another pipeline or version than pipeline.yaml")
    slots = {s["name"]: s for s in pipeline["slots"]}
    reg_slots = registry["implementations"]
    if set(slots) != set(reg_slots):
        errors.append(f"slots differ: {sorted(slots)} vs {sorted(reg_slots)}")
    kinds: dict[tuple[str, str], str] = {}
    levels: dict[tuple[str, str], str] = {}
    for name, slot in slots.items():
        reg = reg_slots.get(name, {})
        allowed = {a["id"]: a for a in slot["implementations"]["allowed"]}
        if set(allowed) != set(reg):
            errors.append(f"slot {name}: implementation ids differ ({sorted(allowed)} vs {sorted(reg)})")
        for impl_id, entry in reg.items():
            kinds[(name, impl_id)] = entry["kind"]
            levels[(name, impl_id)] = validation_level(entry)
            errors += external_errors(name, impl_id, entry, root=root, external=external)
            errors += upstream_errors(name, impl_id, entry, root=root)
            if impl_id in allowed:
                for field in ("step", "image"):
                    if allowed[impl_id][field] != entry[field]:
                        errors.append(f"slot {name}, {impl_id}: {field} differs between the files")
            if entry["kind"] not in ("real", "stub", "planned"):
                errors.append(f"slot {name}, {impl_id}: unknown kind {entry['kind']!r}")
            source = entry["source"]
            if entry["kind"] != "planned" and source.get("repository") != "this":
                if not re.fullmatch(r"[0-9a-f]{40}", str(source.get("commit", ""))):
                    errors.append(f"slot {name}, {impl_id}: an external source needs a 40-hex commit")
    for pname, profile in registry["profiles"].items():
        for name, ids in profile["enabled"].items():
            for impl_id in ids:
                kind = kinds.get((name, impl_id))
                if kind is None:
                    errors.append(f"profile {pname}: unknown implementation {name}/{impl_id}")
                elif kind == "planned":
                    errors.append(f"profile {pname}: planned implementation {name}/{impl_id} is enabled")
                elif kind == "stub" and pname != "ci":
                    errors.append(f"profile {pname}: stub {name}/{impl_id} is enabled outside ci")
                minimum = PROFILE_MIN_VALIDATION.get(pname, DEFAULT_PROFILE_MIN_VALIDATION)
                if kind is not None and level_rank(levels[(name, impl_id)]) < level_rank(minimum):
                    errors.append(
                        f"profile {pname}: {name}/{impl_id} is {levels[(name, impl_id)]}, "
                        f"below the minimum {minimum} (implementation_not_validated)"
                    )
        for name, impl_id in profile["default"].items():
            if impl_id not in profile["enabled"].get(name, []):
                errors.append(f"profile {pname}: default {name}/{impl_id} is not enabled")
    errors += published_errors(registry)
    errors += display_errors(registry, set(slots))
    if presets is not None:
        errors += preset_check(pipeline, registry, presets)
    return errors


def enabled_settings(registry: dict, profile: str) -> list[dict]:
    """One row per allowed implementation: slot, implementation_id, enabled, is_default."""
    spec = registry["profiles"][profile]
    return [
        {
            "slot": slot,
            "implementation_id": impl_id,
            "enabled": impl_id in spec["enabled"].get(slot, []),
            "is_default": spec["default"].get(slot) == impl_id,
        }
        for slot, impls in registry["implementations"].items()
        for impl_id in impls
    ]


def enabled_preset_settings(
    registry: dict, profile: str, preset_ids: Iterable[str] | None = None
) -> list[dict]:
    """One row per preset: preset_id, enabled, is_default, for ``preset_settings``.
    ``preset_ids`` are the presets of the pipeline (default: those the profile enables)."""
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


def materialize(pipeline: dict, registry: dict, digests: dict[str, str]) -> tuple[dict, dict]:
    """Copies of both documents with every image taken from ``digests`` (planned ones excepted)."""
    pipeline, registry = copy.deepcopy(pipeline), copy.deepcopy(registry)
    for impls in registry["implementations"].values():
        for entry in impls.values():
            if entry["kind"] == "planned":
                continue
            key = f"{entry['step']['name']}@{entry['step']['version']}"
            ref = digests.get(key)
            if ref is None and entry.get("build") == "local":
                continue  # built by the deployment itself: keeps the placeholder until it is
            if ref is None:
                raise ValueError(f"no digest supplied for {key}")
            if not DIGEST_REF.match(ref):
                raise ValueError(f"{key}: not a digest-pinned image reference: {ref}")
            entry["image"] = ref
    for slot in pipeline["slots"]:
        reg = registry["implementations"][slot["name"]]
        for allowed in slot["implementations"]["allowed"]:
            allowed["image"] = reg[allowed["id"]]["image"]
    return pipeline, registry


def pin(directory: Path, digests: dict[str, str]) -> list[str]:
    """Write ``digests`` into ``directory``/registry.yaml and pipeline.yaml in place (text
    substitution: comments and layout survive). Returns the pinned ``name@version`` keys.
    Raises ValueError for a bad reference or an unknown step; the files are restored when
    the result does not pass ``check``."""
    reg_path, pipe_path = directory / "registry.yaml", directory / "pipeline.yaml"
    originals = {p: p.read_text(encoding="utf-8") for p in (reg_path, pipe_path)}
    _, reg = load(directory)
    by_key: dict[str, list[dict]] = {}
    for impls in reg["implementations"].values():
        for entry in impls.values():
            by_key.setdefault(f"{entry['step']['name']}@{entry['step']['version']}", []).append(entry)
    unknown = sorted(set(digests) - set(by_key))
    if unknown:
        raise ValueError(f"not steps of the registry: {unknown}")
    replacements: dict[str, str] = {}
    for key, ref in digests.items():
        for entry in by_key[key]:
            if entry["kind"] == "planned":
                raise ValueError(f"{key}: a planned implementation has no image to pin")
            if entry.get("build") == "local":
                raise ValueError(f"{key}: build: local entries are never pinned to a published digest")
            if not DIGEST_REF.match(ref) or ref.endswith(PLACEHOLDER):
                raise ValueError(f"{key}: not a real digest-pinned image reference: {ref}")
            old = str(entry["image"])
            if ref.split("@", 1)[0] != old.split("@", 1)[0]:
                raise ValueError(
                    f"{key}: {ref.split('@', 1)[0]} is not the repository of the entry "
                    f"({old.split('@', 1)[0]})"
                )
            replacements[old] = ref
    texts = dict(originals)
    for old, new in replacements.items():
        for path in (reg_path, pipe_path):
            if old not in texts[path]:
                raise ValueError(f"{path.name}: {old} not found")
            texts[path] = texts[path].replace(old, new)
    # published: true once the deployment profile is fully pinned
    if not published_errors({**yaml.safe_load(texts[reg_path]), "published": True}):
        flag = "published: true"
        if re.search(r"^published:.*$", texts[reg_path], re.M):
            texts[reg_path] = re.sub(r"^published:.*$", flag, texts[reg_path], count=1, flags=re.M)
        else:
            texts[reg_path] = re.sub(
                r"^(pipeline: \{.*\})$", lambda m: m.group(1) + "\n" + flag, texts[reg_path], count=1, flags=re.M
            )
    for path, text in texts.items():
        path.write_text(text, encoding="utf-8", newline="")
    problems = check(*load(directory), load_presets(directory))
    if problems:
        for path, text in originals.items():
            path.write_text(text, encoding="utf-8", newline="")
        raise ValueError("the pinned files do not pass check: " + "; ".join(problems))
    return sorted(digests)


def sources(registry: dict, *, local: bool = False) -> dict[str, dict]:
    """Unique steps to build: ``"<name>@<version>"`` -> source entry. Planned entries are
    excluded, and so are ``build: local`` ones (their image is never pushed anywhere by this
    tool; a deployment builds them itself) unless ``local`` is set."""
    out: dict[str, dict] = {}
    for impls in registry["implementations"].values():
        for entry in impls.values():
            if entry["kind"] == "planned" or (entry.get("build") == "local" and not local):
                continue
            out[f"{entry['step']['name']}@{entry['step']['version']}"] = entry["source"]
    return out


def _external_dir(root: Path, repository: str) -> Path:
    return root / repository.split("/")[-1]


def _run(*cmd: str, capture: bool = False) -> str:
    print("+", " ".join(cmd), flush=True)
    done = subprocess.run(cmd, check=True, text=True, capture_output=capture)
    return done.stdout.strip() if capture else ""


def fetch_targets(registry: dict) -> dict[str, str]:
    """``{repository: commit}`` to fetch: the sources of external steps and the upstream
    repositories of this repository's wrappers (``source.upstream``). Two entries that name
    one repository at different commits are an error (one directory per repository)."""
    out: dict[str, str] = {}
    for source in sources(registry).values():
        pins = []
        if source["repository"] != "this":
            pins.append((source["repository"], source["commit"]))
        if source.get("upstream"):
            pins.append((source["upstream"]["repository"], source["upstream"]["commit"]))
        for repository, commit in pins:
            if out.setdefault(repository, commit) != commit:
                raise SystemExit(f"{repository}: pinned at two commits ({out[repository]}, {commit})")
    return out


def fetch(registry: dict, dest: Path) -> None:
    for repository, commit in fetch_targets(registry).items():
        target = _external_dir(dest, repository)
        if target.exists():
            continue
        target.mkdir(parents=True)
        url = f"https://github.com/{repository}.git"
        _run("git", "-C", str(target), "init", "-q")
        _run("git", "-C", str(target), "fetch", "-q", "--depth", "1", url, commit)
        _run("git", "-C", str(target), "checkout", "-q", "FETCH_HEAD")
        head = _run("git", "-C", str(target), "rev-parse", "HEAD", capture=True)
        if head != commit:
            raise SystemExit(f"{repository}: fetched {head}, pinned {commit}")


def build(registry: dict, external: Path, prefix: str, manifests_out: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for key, source in sources(registry).items():
        name, version = key.split("@")
        context = REPO_ROOT if source["repository"] == "this" else _external_dir(external, source["repository"])
        tag = f"{prefix}/{name}:ci"
        _run("docker", "build", "-f", str(context / source["dockerfile"]), "-t", tag, str(context))
        _run("docker", "push", tag)
        ref = _run("docker", "image", "inspect", tag, "--format", "{{index .RepoDigests 0}}", capture=True)
        if not DIGEST_REF.match(ref):
            raise SystemExit(f"{key}: unexpected image reference {ref!r}")
        digests[key] = ref
        manifest = _run("docker", "run", "--rm", "--entrypoint", "cat", ref, "/opt/step/step.yaml", capture=True)
        target = manifests_out / f"{name}-{version}" / "step.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(manifest + "\n", encoding="utf-8")
    return digests


def _dump(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False, width=120), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "materialize", "fetch", "build", "pin"):
        sub.add_parser(name).add_argument("--dir", type=Path, default=HERE)
    sub.choices["check"].add_argument(
        "--external",
        type=Path,
        default=None,
        help="fetched external sources (registry.py fetch), to read the manifests of external-origin steps",
    )
    sub.choices["fetch"].add_argument("--dest", type=Path, required=True)
    b = sub.choices["build"]
    b.add_argument("--external", type=Path, required=True)
    b.add_argument("--prefix", required=True)
    b.add_argument("--digests-out", type=Path, required=True)
    b.add_argument("--manifests-out", type=Path, required=True)
    sub.choices["pin"].add_argument("--digests", type=Path, required=True)
    sub.choices["materialize"].add_argument("--digests", type=Path, required=True)
    sub.choices["materialize"].add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    pipeline, registry = load(args.dir)
    errors = check(
        pipeline, registry, load_presets(args.dir), external=getattr(args, "external", None)
    )
    if errors:
        print("\n".join(f"ERROR {e}" for e in errors), file=sys.stderr)
        return 1
    if args.cmd == "check":
        print("OK registry, presets and pipeline agree")
        return 0
    if args.cmd == "fetch":
        fetch(registry, args.dest)
        return 0
    if args.cmd == "build":
        digests = build(registry, args.external, args.prefix, args.manifests_out)
        args.digests_out.write_text(json.dumps(digests, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    digests = json.loads(args.digests.read_text(encoding="utf-8"))
    if args.cmd == "pin":
        try:
            pinned = pin(args.dir, digests)
        except ValueError as exc:
            print(f"ERROR {exc}", file=sys.stderr)
            return 1
        print("pinned: " + ", ".join(pinned))
        print(f"published: {str(load(args.dir)[1].get('published', False)).lower()}")
        return 0
    pipeline, registry = materialize(pipeline, registry, digests)
    _dump(pipeline, args.out / "stroke-lnm" / "pipeline.yaml")
    _dump(registry, args.out / "stroke-lnm" / "registry.yaml")
    presets_out = args.out / "stroke-lnm" / "presets"
    presets_out.mkdir(parents=True, exist_ok=True)
    for path in sorted((args.dir / "presets").glob("*.yaml")):
        shutil.copyfile(path, presets_out / path.name)  # bytes unchanged: runs record their SHA-256
    print(f"written to {args.out / 'stroke-lnm'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
