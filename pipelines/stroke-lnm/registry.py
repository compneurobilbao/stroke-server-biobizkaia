#!/usr/bin/env python3
"""Registry tool of the stroke-lnm pipeline (card F0-33).

    registry.py check [--dir DIR]
        pipeline.yaml and registry.yaml agree (slots, implementation ids, steps, images);
        profiles only enable existing, non-planned implementations; stubs are enabled only
        in the ci profile; defaults are enabled.

    registry.py materialize --digests FILE --out DIR [--dir DIR]
        Write DIR/stroke-lnm/pipeline.yaml and DIR/stroke-lnm/registry.yaml with the image
        of every implementation replaced by the reference in FILE (a JSON object
        {"<step name>@<version>": "<repository>@sha256:<digest>"}). Planned
        implementations keep their placeholder. CI builds the steps and writes the digests
        of the local images; a deployment uses the digests of the published images.

    registry.py fetch --dest DIR
        Fetch the external step sources at the commits pinned in registry.yaml (CI).

    registry.py build --external DIR --prefix HOST/PATH --digests-out FILE --manifests-out DIR
        Build every non-planned step image from this repository or from the fetched
        sources, push it to the (throwaway) registry at the prefix, write the pushed
        references to FILE and the manifest read from each image (/opt/step/step.yaml,
        step-contract.md 1.1) to DIR/<name>-<version>/step.yaml.

``enabled_settings(registry, profile)`` gives the rows for ``slot_implementation_settings``
of a profile (the admin endpoints of F0-30 edit the same table in a running deployment).
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
DIGEST_REF = re.compile(
    r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]+([._-][a-z0-9]+)*)+@sha256:[a-f0-9]{64}$"
)
PLACEHOLDER = "@sha256:" + "0" * 64


def load(directory: Path = HERE) -> tuple[dict, dict]:
    pipeline = yaml.safe_load((directory / "pipeline.yaml").read_text(encoding="utf-8"))
    registry = yaml.safe_load((directory / "registry.yaml").read_text(encoding="utf-8"))
    return pipeline, registry


def check(pipeline: dict, registry: dict) -> list[str]:
    errors: list[str] = []
    ident = {"id": pipeline["id"], "version": pipeline["version"]}
    if registry.get("pipeline") != ident:
        errors.append("registry.yaml names another pipeline or version than pipeline.yaml")
    slots = {s["name"]: s for s in pipeline["slots"]}
    reg_slots = registry["implementations"]
    if set(slots) != set(reg_slots):
        errors.append(f"slots differ: {sorted(slots)} vs {sorted(reg_slots)}")
    kinds: dict[tuple[str, str], str] = {}
    for name, slot in slots.items():
        reg = reg_slots.get(name, {})
        allowed = {a["id"]: a for a in slot["implementations"]["allowed"]}
        if set(allowed) != set(reg):
            errors.append(f"slot {name}: implementation ids differ ({sorted(allowed)} vs {sorted(reg)})")
        for impl_id, entry in reg.items():
            kinds[(name, impl_id)] = entry["kind"]
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
        for name, impl_id in profile["default"].items():
            if impl_id not in profile["enabled"].get(name, []):
                errors.append(f"profile {pname}: default {name}/{impl_id} is not enabled")
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


def materialize(pipeline: dict, registry: dict, digests: dict[str, str]) -> tuple[dict, dict]:
    """Copies of both documents with every image taken from ``digests`` (planned ones excepted)."""
    pipeline, registry = copy.deepcopy(pipeline), copy.deepcopy(registry)
    for impls in registry["implementations"].values():
        for entry in impls.values():
            if entry["kind"] == "planned":
                continue
            key = f"{entry['step']['name']}@{entry['step']['version']}"
            ref = digests.get(key)
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


REPO_ROOT = HERE.parents[1]


def sources(registry: dict) -> dict[str, dict]:
    """Unique steps to build: ``"<name>@<version>"`` -> source entry (planned ones excluded)."""
    out: dict[str, dict] = {}
    for impls in registry["implementations"].values():
        for entry in impls.values():
            if entry["kind"] != "planned":
                out[f"{entry['step']['name']}@{entry['step']['version']}"] = entry["source"]
    return out


def _external_dir(root: Path, repository: str) -> Path:
    return root / repository.split("/")[-1]


def _run(*cmd: str, capture: bool = False) -> str:
    print("+", " ".join(cmd), flush=True)
    done = subprocess.run(cmd, check=True, text=True, capture_output=capture)
    return done.stdout.strip() if capture else ""


def fetch(registry: dict, dest: Path) -> None:
    for source in sources(registry).values():
        if source["repository"] == "this":
            continue
        target = _external_dir(dest, source["repository"])
        if target.exists():
            continue
        target.mkdir(parents=True)
        url = f"https://github.com/{source['repository']}.git"
        _run("git", "-C", str(target), "init", "-q")
        _run("git", "-C", str(target), "fetch", "-q", "--depth", "1", url, source["commit"])
        _run("git", "-C", str(target), "checkout", "-q", "FETCH_HEAD")
        head = _run("git", "-C", str(target), "rev-parse", "HEAD", capture=True)
        if head != source["commit"]:
            raise SystemExit(f"{source['repository']}: fetched {head}, pinned {source['commit']}")


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
    for name in ("check", "materialize", "fetch", "build"):
        sub.add_parser(name).add_argument("--dir", type=Path, default=HERE)
    sub.choices["fetch"].add_argument("--dest", type=Path, required=True)
    b = sub.choices["build"]
    b.add_argument("--external", type=Path, required=True)
    b.add_argument("--prefix", required=True)
    b.add_argument("--digests-out", type=Path, required=True)
    b.add_argument("--manifests-out", type=Path, required=True)
    sub.choices["materialize"].add_argument("--digests", type=Path, required=True)
    sub.choices["materialize"].add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    pipeline, registry = load(args.dir)
    errors = check(pipeline, registry)
    if errors:
        print("\n".join(f"ERROR {e}" for e in errors), file=sys.stderr)
        return 1
    if args.cmd == "check":
        print("OK registry and pipeline agree")
        return 0
    if args.cmd == "fetch":
        fetch(registry, args.dest)
        return 0
    if args.cmd == "build":
        digests = build(registry, args.external, args.prefix, args.manifests_out)
        args.digests_out.write_text(json.dumps(digests, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    digests = json.loads(args.digests.read_text(encoding="utf-8"))
    pipeline, registry = materialize(pipeline, registry, digests)
    _dump(pipeline, args.out / "stroke-lnm" / "pipeline.yaml")
    _dump(registry, args.out / "stroke-lnm" / "registry.yaml")
    print(f"written to {args.out / 'stroke-lnm'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
