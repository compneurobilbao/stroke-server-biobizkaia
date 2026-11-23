#!/usr/bin/env python3
"""Publish the step images of the stroke-lnm pipeline (card REL-01).

    release.py plan [--steps all|NAME,NAME]
        Print the steps a release would build (JSON): name, version, image repository,
        source. `build: local` and planned entries never appear.

    release.py run --external DIR --work DIR --digests-out FILE --report-out FILE
                   [--steps all|NAME,NAME] [--dry-run] [--namespace docker.io/ORG]
                   [--dest-override localhost:PORT/PATH]
        For each selected step: prepare its reference case, build the image from the pinned
        source (this repository, or the external repository fetched by ``registry.py fetch``
        into DIR), check the manifest inside the image, run the step's reference test
        against that image and (unless --dry-run) push that same image to
        ``<repository>:<step version>`` on the public registry, reading the digest back from the
        registry. The tag is immutable: a tag that already exists is never overwritten (it is
        checked before the build and again right before the push; an existing tag is an
        error at the second check). A step whose tag already exists before the build is not
        rebuilt: its published digest is recorded, status ``already-published``.

        FILE (``--digests-out``) is the digests file of ``registry.py pin`` and
        ``registry.py materialize`` (``{"<name>@<version>": "<repository>@sha256:<digest>"}``);
        it holds published references only, never the digests of a dry run.
        ``--report-out`` is a JSON report with the status of every step.

``--dest-override`` publishes below a registry on localhost instead (CI rehearsal of the
publishing path against a second throwaway registry; refused for any other host).

The registry's image repository of each entry is the destination; ``--namespace`` is a guard
(every destination must be below it, default ``docker.io/compneurobilbaolab``).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]

_spec = importlib.util.spec_from_file_location(
    "stroke_lnm_registry_release", HERE / "registry.py"
)
registry = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = registry
_spec.loader.exec_module(registry)  # type: ignore[union-attr]

DEFAULT_NAMESPACE = "docker.io/compneurobilbaolab"
SHA = re.compile(r"sha256:[a-f0-9]{64}")
# Messages of `docker buildx imagetools inspect` for a tag that does not exist (yet). Anything
# else (authentication, network) stops the release: an unknown state is never treated as free.
NOT_FOUND = (
    "not found",
    "no such manifest",
    "manifest unknown",
    "name unknown",
    "repository does not exist",
)

# How the reference case of a step is made before its reference test runs (placeholders:
# {repo} this repository, {external} the fetched sources, {step} the step directory,
# {contracts} the contracts directory, {refdata} download cache of public reference data).
# Each command mirrors the one that the step's own CI runs. A step whose manifest declares a
# `reference_test.generator` needs no entry (the SDK runs it). Reference bundles of a step are
# read from {step}/tests/reference/bundle, which every generator below writes.
PREPARE: dict[str, list[list[str]]] = {
    "lesion-qc": [["python", "{step}/tests/make_reference.py"]],
    "research-report": [["python", "{step}/tests/make_reference.py"]],
    "cognet-stroke": [
        [
            "python",
            "{step}/tests/make_reference.py",
            "{step}/tests/reference",
            "{contracts}",
        ]
    ],
    "lnm-tracts": [
        [
            "python",
            "{step}/tests/make_reference.py",
            "{contracts}",
            "{step}/tests/reference",
        ]
    ],
    "ct-lesion-to-mni152": [
        [
            "bash",
            "{repo}/pipelines/stroke-lnm/ci/ct_reference.sh",
            "{external}",
            "{refdata}",
            "{step}/tests/reference",
        ]
    ],
    "flnm-fast": [],  # reference_test.generator in the manifest
    "fdisconnection-stub": [],  # synthetic stub: no reference test (kind: stub only)
}
BUNDLE_SUBDIR = Path("tests") / "reference" / "bundle"

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class ReleaseError(RuntimeError):
    pass


@dataclass
class Target:
    key: str
    name: str
    version: str
    repository: str  # destination without tag, e.g. docker.io/org/step-name
    source: dict
    kind: str

    def tag_in(self, ctx: "Context") -> str:
        return f"{ctx.destination(self)}:{self.version}"


@dataclass
class Context:
    repo_root: Path
    external: Path
    work: Path
    dry_run: bool
    namespace: str
    revision: str = ""
    dest_override: str = ""
    run: Runner = field(default=subprocess.run)

    def destination(self, t: Target) -> str:
        """Repository the step is published to: the registry's, or the localhost override."""
        if not self.dest_override:
            return t.repository
        prefix = self.namespace.rstrip("/") + "/"
        return self.dest_override.rstrip("/") + "/" + t.repository[len(prefix) :]


def image_repository(image: str) -> str:
    return image.split("@", 1)[0]


def targets(
    reg: dict, selection: list[str] | None = None, namespace: str = DEFAULT_NAMESPACE
) -> list[Target]:
    """Steps to release, one per ``name@version``, in registry order. ``selection`` is a list
    of step names (None: all). ``build: local`` and planned entries are never targets, and
    selecting one by name is an error."""
    found: dict[str, Target] = {}
    local_names: set[str] = set()
    known: set[str] = set()
    for impls in reg["implementations"].values():
        for entry in impls.values():
            name = entry["step"]["name"]
            known.add(name)
            if entry["kind"] == "planned":
                continue
            if entry.get("build") == "local":
                local_names.add(name)
                continue
            key = f"{name}@{entry['step']['version']}"
            repository = image_repository(entry["image"])
            if not repository.startswith(namespace.rstrip("/") + "/"):
                raise ReleaseError(
                    f"{key}: image repository {repository} is outside {namespace}"
                )
            found.setdefault(
                key,
                Target(
                    key,
                    name,
                    entry["step"]["version"],
                    repository,
                    entry["source"],
                    entry["kind"],
                ),
            )
    if selection is not None:
        for name in selection:
            if name in local_names and name not in {t.name for t in found.values()}:
                raise ReleaseError(f"{name}: build: local entries are never published")
            if name not in known:
                raise ReleaseError(
                    f"{name}: not a step of the registry (known: {sorted(known)})"
                )
            if name not in {t.name for t in found.values()}:
                raise ReleaseError(f"{name}: nothing to publish (planned entry)")
        return [t for t in found.values() if t.name in selection]
    return list(found.values())


def parse_selection(text: str) -> list[str] | None:
    names = [n.strip() for n in re.split(r"[,\s]+", text) if n.strip()]
    return None if not names or names == ["all"] else names


# --------------------------------------------------------------------------- docker helpers


def _run(
    ctx: Context, *cmd: str, check: bool = True, env: dict | None = None
) -> "subprocess.CompletedProcess[str]":
    print("+", " ".join(cmd), flush=True)
    done = ctx.run(cmd, text=True, capture_output=True, check=False, env=env)
    if done.stdout:
        print(done.stdout, end="" if done.stdout.endswith("\n") else "\n", flush=True)
    if done.stderr:
        print(
            done.stderr,
            end="" if done.stderr.endswith("\n") else "\n",
            file=sys.stderr,
            flush=True,
        )
    if check and done.returncode != 0:
        raise ReleaseError(f"command failed ({done.returncode}): {' '.join(cmd)}")
    return done


def _stream(
    ctx: Context, *cmd: str, env: dict | None = None, cwd: Path | None = None
) -> None:
    """Run a long command with live output (build, reference test, preparation)."""
    print("+", " ".join(cmd), flush=True)
    done = ctx.run(cmd, text=True, check=False, env=env, cwd=cwd)
    if done.returncode != 0:
        raise ReleaseError(f"command failed ({done.returncode}): {' '.join(cmd)}")


def remote_digest(ctx: Context, ref: str) -> str | None:
    """Manifest digest of ``ref`` on its registry, None if the tag does not exist.
    Raises on any other failure."""
    done = _run(
        ctx,
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        ref,
        "--format",
        "{{json .Manifest}}",
        check=False,
    )
    if done.returncode == 0:
        digest = json.loads(done.stdout).get("digest", "")
        if not SHA.fullmatch(digest):
            raise ReleaseError(f"{ref}: unexpected manifest digest {digest!r}")
        return digest
    text = (done.stderr + done.stdout).lower()
    if any(m in text for m in NOT_FOUND):
        return None
    raise ReleaseError(
        f"{ref}: cannot tell whether the tag exists (authentication or network?): {text.strip()[:300]}"
    )


def _expand(parts: list[str], values: dict[str, str]) -> list[str]:
    return [p.format(**values) for p in parts]


def step_dir(t: Target, ctx: Context) -> Path:
    stem = Path(t.source["dockerfile"]).parent
    base = (
        ctx.repo_root
        if t.source["repository"] == "this"
        else registry._external_dir(ctx.external, t.source["repository"])
    )
    return base / stem


def prepare_reference(t: Target, ctx: Context, manifest: dict) -> None:
    sdir = step_dir(t, ctx)
    generator = (manifest.get("reference_test") or {}).get("generator")
    if t.name not in PREPARE and not generator:
        raise ReleaseError(
            f"{t.key}: no reference preparation known; add the step to PREPARE in release.py "
            "or declare reference_test.generator in its manifest"
        )
    values = {
        "repo": str(ctx.repo_root),
        "external": str(ctx.external),
        "step": str(sdir),
        "contracts": str(ctx.repo_root / "contracts"),
        "refdata": str(ctx.work / "refdata"),
    }
    (ctx.work / "refdata").mkdir(parents=True, exist_ok=True)
    for cmd in PREPARE.get(t.name, []):
        expanded = _expand(cmd, values)
        if expanded[0] == "python":
            expanded[0] = sys.executable
        _stream(ctx, *expanded)


def load_step_manifest(t: Target, ctx: Context) -> dict:
    import yaml

    path = step_dir(t, ctx) / "step.yaml"
    if not path.is_file():
        raise ReleaseError(f"{t.key}: manifest {path} not found")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def check_image_manifest(t: Target, ctx: Context, local: str) -> None:
    """The manifest inside the image is the step the registry names (name and version)."""
    import yaml

    done = _run(
        ctx,
        "docker",
        "run",
        "--rm",
        "--entrypoint",
        "cat",
        local,
        "/opt/step/step.yaml",
    )
    manifest = yaml.safe_load(done.stdout) or {}
    if (manifest.get("name"), manifest.get("version")) != (t.name, t.version):
        raise ReleaseError(
            f"{t.key}: the image declares {manifest.get('name')} {manifest.get('version')}"
        )


def reference_test(t: Target, ctx: Context, local: str, manifest: dict) -> str:
    rt = manifest.get("reference_test")
    if not rt:
        if t.kind == "stub":
            return "skipped (a stub has no reference test)"
        raise ReleaseError(
            f"{t.key}: a real step needs a reference_test in its manifest"
        )
    sdir = step_dir(t, ctx)
    cmd = [
        "stroke-step-reftest", "--step-dir", str(sdir), "--mode", "docker", "--image", local,
        "--workdir", str(ctx.work / "reftest" / f"{t.name}-{t.version}"),
    ]  # fmt: skip
    for bundle in rt.get("bundles") or {}:
        directory = sdir / BUNDLE_SUBDIR
        if not directory.is_dir():
            raise ReleaseError(
                f"{t.key}: reference bundle {bundle} expected in {directory}"
            )
        cmd += ["--bundle", f"{bundle}={directory}"]
    _stream(ctx, *cmd)
    return "passed"


def build_image(t: Target, ctx: Context, local: str) -> None:
    context = (
        ctx.repo_root
        if t.source["repository"] == "this"
        else registry._external_dir(ctx.external, t.source["repository"])
    )
    cmd = [
        "docker", "build", "-f", str(context / t.source["dockerfile"]), "-t", local,
        "--label", f"org.opencontainers.image.title=step-{t.name}",
        "--label", f"org.opencontainers.image.version={t.version}",
    ]  # fmt: skip
    if ctx.revision:
        cmd += ["--label", f"org.opencontainers.image.revision={ctx.revision}"]
    _stream(ctx, *cmd, str(context))


def publish(t: Target, ctx: Context, local: str) -> tuple[str, str]:
    """Push the tested local image to the public tag, never overwriting an existing one.
    Returns (status, manifest digest as read back from the registry)."""
    tag = t.tag_in(ctx)
    existing = remote_digest(ctx, tag)  # again: the tag may have appeared during the build
    if existing is not None:
        raise ReleaseError(
            f"{t.key}: {tag} already exists (digest {existing}) and this build was not "
            "pushed. Tags are immutable: release a new step version instead."
        )
    _run(ctx, "docker", "tag", local, tag)
    _run(ctx, "docker", "push", tag)
    repository = ctx.destination(t)
    refs = _run(
        ctx, "docker", "image", "inspect", tag, "--format", "{{range .RepoDigests}}{{println .}}{{end}}"
    ).stdout.split()
    mine = [r for r in refs if r.startswith(repository + "@")]
    match = SHA.search(mine[0]) if mine else None
    if not match:
        raise ReleaseError(f"{t.key}: no digest of {repository} after the push ({refs})")
    digest = match.group(0)
    after = remote_digest(ctx, tag)
    if after != digest:
        raise ReleaseError(f"{t.key}: {tag} has digest {after} after the push, the push reported {digest}")
    return "published", digest


def release_one(t: Target, ctx: Context) -> dict:
    """Build, test and (unless dry run) publish one step. Returns its report entry."""
    tag = t.tag_in(ctx)
    entry = {"step": t.key, "tag": tag, "kind": t.kind}
    # An existing tag is never rebuilt or overwritten.
    try:
        existing = remote_digest(ctx, tag)
    except ReleaseError as exc:
        if not ctx.dry_run:
            raise
        existing = None
        entry["remote_check"] = f"not possible without credentials ({str(exc)[:80]}...)"
    if existing is not None:
        entry.update(
            status="already-published",
            digest=existing,
            reference_test="not run (not rebuilt)",
        )
        entry["reference"] = f"{ctx.destination(t)}@{existing}"
        return entry

    manifest = load_step_manifest(t, ctx)
    local = f"stroke-release/{t.name}:{t.version}"
    if t.kind != "stub":
        prepare_reference(t, ctx, manifest)
    build_image(t, ctx, local)
    check_image_manifest(t, ctx, local)
    entry["reference_test"] = reference_test(t, ctx, local, manifest)
    if ctx.dry_run:
        entry["status"] = "dry-run"
        entry["image_id"] = _run(ctx, "docker", "image", "inspect", local, "--format", "{{.Id}}").stdout.strip()
        entry["note"] = "built and tested, not pushed to the public registry"
        return entry
    entry["status"], digest = publish(t, ctx, local)
    entry["digest"] = digest
    entry["reference"] = f"{ctx.destination(t)}@{digest}"
    return entry


def run_release(
    selection: list[str] | None, ctx: Context
) -> tuple[dict[str, str], list[dict]]:
    reg = registry.load(HERE)[1]
    todo = targets(reg, selection, ctx.namespace)
    if not todo:
        raise ReleaseError("nothing to release")
    digests: dict[str, str] = {}
    report: list[dict] = []
    for t in todo:
        print(
            f"\n=== {t.key} -> {t.tag_in(ctx)} ({'dry run' if ctx.dry_run else 'publish'})",
            flush=True,
        )
        try:
            entry = release_one(t, ctx)
        except ReleaseError as exc:
            report.append(
                {"step": t.key, "tag": t.tag_in(ctx), "status": "failed", "error": str(exc)}
            )
            raise ReleaseFailed(digests, report) from exc
        report.append(entry)
        if "reference" in entry:
            digests[t.key] = entry["reference"]
    return digests, report


class ReleaseFailed(ReleaseError):
    """A step failed; what was published before it is kept in ``digests`` and ``report``."""

    def __init__(self, digests: dict[str, str], report: list[dict]):
        super().__init__(report[-1]["error"])
        self.digests, self.report = digests, report


def summary_markdown(report: list[dict], dry_run: bool) -> str:
    lines = [
        f"### Step image release ({'DRY RUN, nothing pushed' if dry_run else 'public registry'})",
        "",
        "| Step | Status | Reference test | Image |",
        "|---|---|---|---|",
    ]
    for e in report:
        image = e.get("reference") or (
            f"`{e['image_id']}` (local image id, not pushed)" if e.get("image_id") else ""
        )
        lines.append(
            f"| {e['step']} | {e['status']} | {e.get('reference_test', '')} | "
            f"{('`' + image + '`') if e.get('reference') else image} |"
        )
        if e.get("error"):
            lines.append(f"|  | error: {e['error']} | | |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--steps", default="all")
    plan.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    run = sub.add_parser("run")
    run.add_argument("--steps", default="all")
    run.add_argument("--namespace", default=DEFAULT_NAMESPACE)
    run.add_argument("--external", type=Path, required=True)
    run.add_argument("--work", type=Path, required=True)
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--dest-override", default="", help="publish below this localhost registry (CI rehearsal)")
    run.add_argument("--digests-out", type=Path, required=True)
    run.add_argument("--report-out", type=Path, required=True)
    run.add_argument(
        "--summary-out",
        type=Path,
        help="Markdown summary (e.g. $GITHUB_STEP_SUMMARY), appended",
    )
    args = ap.parse_args(argv)

    pipeline, reg = registry.load(HERE)
    errors = registry.check(
        pipeline,
        reg,
        registry.load_presets(HERE),
        external=getattr(args, "external", None),
    )
    if errors:
        print("\n".join(f"ERROR {e}" for e in errors), file=sys.stderr)
        return 1
    try:
        selection = parse_selection(args.steps)
        if args.cmd == "plan":
            todo = targets(reg, selection, args.namespace)
            print(json.dumps([t.__dict__ for t in todo], indent=2))
            return 0
        if args.dest_override and not re.match(r"^(localhost|127[.]0[.]0[.]1):[0-9]+(/|$)", args.dest_override):
            raise ReleaseError("--dest-override must be a registry on localhost")
        ctx = Context(
            repo_root=REPO_ROOT,
            external=args.external.resolve(),
            work=args.work.resolve(),
            dry_run=args.dry_run,
            namespace=args.namespace,
            revision=os.environ.get("GITHUB_SHA", ""),
            dest_override=args.dest_override,
        )
        ctx.work.mkdir(parents=True, exist_ok=True)
        failure: ReleaseFailed | None = None
        try:
            digests, report = run_release(selection, ctx)
        except ReleaseFailed as exc:
            failure, digests, report = exc, exc.digests, exc.report
        args.digests_out.parent.mkdir(parents=True, exist_ok=True)
        args.digests_out.write_text(
            json.dumps(digests, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        args.report_out.write_text(
            json.dumps(report, indent=2) + "\n", encoding="utf-8"
        )
        text = summary_markdown(report, args.dry_run)
        print(text)
        if args.summary_out:
            with open(args.summary_out, "a", encoding="utf-8") as fh:
                fh.write(text)
        if failure:
            print(f"ERROR {failure}", file=sys.stderr)
            return 1
        return 0
    except ReleaseError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
