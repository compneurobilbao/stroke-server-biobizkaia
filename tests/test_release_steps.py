"""Release of the step images (card REL-01): `release.py` logic with a fake docker, and the
`registry.py pin` / `published` rules. Nothing here runs docker or touches a registry.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HERE = REPO / "pipelines" / "stroke-lnm"
sys.path.insert(0, str(REPO))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


registry_tool = _load("stroke_lnm_registry_rel01", HERE / "registry.py")
release = _load("stroke_lnm_release_rel01", HERE / "release.py")

NS = "docker.io/compneurobilbaolab"
D = {c: "sha256:" + c * 64 for c in "abcdef"}


def digest_ref(name: str, c: str = "a") -> str:
    return f"{NS}/step-{name}@{D[c]}"


@pytest.fixture()
def tree(tmp_path):
    out = tmp_path / "stroke-lnm"
    shutil.copytree(
        HERE, out, ignore=shutil.ignore_patterns("__pycache__", "tests", "ci")
    )
    return out


def all_digests(registry: dict, c: str = "a") -> dict[str, str]:
    out = {}
    for impls in registry["implementations"].values():
        for entry in impls.values():
            if entry["kind"] != "planned" and entry.get("build") != "local":
                repo = entry["image"].split("@")[0]
                out[f"{entry['step']['name']}@{entry['step']['version']}"] = (
                    f"{repo}@{D[c]}"
                )
    return out


# --------------------------------------------------------------------------- targets


def test_targets_cover_the_registry_once_per_step():
    reg = registry_tool.load(HERE)[1]
    keys = [t.key for t in release.targets(reg)]
    assert len(keys) == len(set(keys))
    assert "research-report@0.3.0" in keys and "flnm-fast@2.0.0" in keys
    assert all(t.repository.startswith(NS + "/step-") for t in release.targets(reg))


def test_selection_by_name_and_unknown_name():
    reg = registry_tool.load(HERE)[1]
    assert [t.name for t in release.targets(reg, ["lesion-qc", "lnm-tracts"])] == [
        "lesion-qc",
        "lnm-tracts",
    ]
    with pytest.raises(release.ReleaseError, match="not a step"):
        release.targets(reg, ["nope"])


def test_build_local_entries_are_never_targets():
    reg = copy.deepcopy(registry_tool.load(HERE)[1])
    entry = reg["implementations"]["functional-disconnection"]["flnm-fast"]
    entry["build"] = "local"
    entry["image"] = "registry.internal.example/step-flnm-fast@" + D["a"]
    assert "flnm-fast" not in [t.name for t in release.targets(reg)]
    with pytest.raises(release.ReleaseError, match="never published"):
        release.targets(reg, ["flnm-fast"])


def test_destination_outside_the_namespace_is_refused():
    reg = copy.deepcopy(registry_tool.load(HERE)[1])
    reg["implementations"]["lesion-intake"]["lesion-qc"]["image"] = (
        "ghcr.io/other/step-lesion-qc@" + D["a"]
    )
    with pytest.raises(release.ReleaseError, match="outside"):
        release.targets(reg)


def test_parse_selection():
    assert (
        release.parse_selection("all") is None and release.parse_selection("") is None
    )
    assert release.parse_selection("lesion-qc, lnm-tracts") == [
        "lesion-qc",
        "lnm-tracts",
    ]


# --------------------------------------------------------------------------- release flow


class FakeDocker:
    """Records commands and answers the few that decide the flow."""

    def __init__(self, remote: dict[str, str] | None = None, built: str = D["a"]):
        self.remote = dict(remote or {})  # tag -> digest
        self.built = built
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        cmd = list(cmd)
        self.calls.append(cmd)

        def done(out="", err="", rc=0):
            return subprocess.CompletedProcess(cmd, rc, out, err)

        if cmd[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            tag = cmd[4]
            if tag in self.remote:
                return done(json.dumps({"digest": self.remote[tag]}))
            return done(err=f"{tag}: not found", rc=1)
        if cmd[:2] == ["docker", "push"]:
            self.remote[cmd[2]] = self.built
            return done()
        if cmd[:3] == ["docker", "image", "inspect"]:
            if "{{.Id}}" in cmd:
                return done("sha256:" + "9" * 64 + "\n")
            repository = cmd[3].rsplit(":", 1)[0]
            # like Docker: Docker Hub references are printed without "docker.io/"
            if repository.startswith("docker.io/"):
                repository = repository[len("docker.io/") :]
            return done(f"{repository}@{self.built}\n")
        if cmd[:2] == ["docker", "run"] and cmd[-1] == "/opt/step/step.yaml":
            image = cmd[-2]
            name, version = image.split("/")[-1].split(":")
            return done(f"name: {name}\nversion: {version}\n")
        return done()

    def pushed(self) -> list[str]:
        return [" ".join(c) for c in self.calls if c[:2] == ["docker", "push"]]


def make_ctx(tmp_path, fake, dry_run=False):
    external = tmp_path / "external"
    return release.Context(
        repo_root=REPO,
        external=external,
        work=tmp_path / "work",
        dry_run=dry_run,
        namespace=NS,
        run=fake,
    )


@pytest.fixture()
def lesion_qc():
    reg = registry_tool.load(HERE)[1]
    return release.targets(reg, ["lesion-qc"])[0]


def test_publish_pushes_the_tag_and_records_the_digest(tmp_path, lesion_qc):
    fake = FakeDocker()
    entry = release.release_one(lesion_qc, make_ctx(tmp_path, fake))
    assert entry["status"] == "published"
    assert entry["reference"] == f"{NS}/step-lesion-qc@{D['a']}"
    assert fake.remote[f"{NS}/step-lesion-qc:1.0.0"] == D["a"]
    # the reference test ran against the built image before anything went to the public registry
    cmds = [" ".join(c) for c in fake.calls]
    assert any(c.startswith("docker build") for c in cmds)
    # order: build, then the reference test (stub of the SDK runner), then the push
    assert cmds.index(
        next(c for c in cmds if c.startswith("docker build"))
    ) < cmds.index(next(c for c in cmds if c.startswith("docker push")))
    assert any(c.startswith("stroke-step-reftest") for c in cmds)


def test_dry_run_never_pushes(tmp_path, lesion_qc):
    fake = FakeDocker()
    entry = release.release_one(lesion_qc, make_ctx(tmp_path, fake, dry_run=True))
    assert entry["status"] == "dry-run" and "reference" not in entry
    assert not fake.remote
    assert not fake.pushed()


def test_existing_tag_is_not_rebuilt_nor_overwritten(tmp_path, lesion_qc):
    tag = f"{NS}/step-lesion-qc:1.0.0"
    fake = FakeDocker(remote={tag: D["b"]})
    entry = release.release_one(lesion_qc, make_ctx(tmp_path, fake))
    assert entry["status"] == "already-published"
    assert entry["reference"] == f"{NS}/step-lesion-qc@{D['b']}"
    assert not fake.pushed()
    assert not any(c[:2] == ["docker", "build"] for c in fake.calls)


def test_tag_that_appears_during_the_build_is_refused(tmp_path, lesion_qc):
    """Race: the tag does not exist at the start and exists when the push is about to happen."""
    tag = f"{NS}/step-lesion-qc:1.0.0"
    fake = FakeDocker(built=D["a"])
    ctx = make_ctx(tmp_path, fake)
    original = release.publish

    def late(t, c, local):
        fake.remote[tag] = D["c"]
        return original(t, c, local)

    release.publish, saved = late, release.publish
    try:
        with pytest.raises(release.ReleaseError, match="immutable"):
            release.release_one(lesion_qc, ctx)
    finally:
        release.publish = saved
    assert not fake.pushed()
    assert fake.remote[tag] == D["c"]


def test_unknown_remote_state_stops_a_real_release(tmp_path, lesion_qc):
    def denied(cmd, **kwargs):
        if list(cmd)[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            return subprocess.CompletedProcess(
                cmd, 1, "", "unauthorized: authentication required"
            )
        return subprocess.CompletedProcess(cmd, 0, "", "")

    with pytest.raises(release.ReleaseError, match="cannot tell"):
        release.release_one(lesion_qc, make_ctx(tmp_path, denied))
    # a dry run does not need credentials
    fake_ok = FakeDocker()
    original = fake_ok.__call__

    def dry(cmd, **kwargs):
        if list(cmd)[:4] == ["docker", "buildx", "imagetools", "inspect"]:
            return subprocess.CompletedProcess(
                cmd, 1, "", "unauthorized: authentication required"
            )
        return original(cmd, **kwargs)

    entry = release.release_one(lesion_qc, make_ctx(tmp_path, dry, dry_run=True))
    assert entry["status"] == "dry-run" and "remote_check" in entry


def test_image_with_the_wrong_manifest_is_refused(tmp_path, lesion_qc):
    fake = FakeDocker()
    original = fake.__call__

    def wrong(cmd, **kwargs):
        if (
            list(cmd)[:2] == ["docker", "run"]
            and list(cmd)[-1] == "/opt/step/step.yaml"
        ):
            return subprocess.CompletedProcess(
                cmd, 0, "name: lesion-qc\nversion: 9.9.9\n", ""
            )
        return original(cmd, **kwargs)

    with pytest.raises(release.ReleaseError, match="declares"):
        release.release_one(lesion_qc, make_ctx(tmp_path, wrong))
    assert not fake.remote


def test_every_real_step_has_a_reference_preparation():
    reg = registry_tool.load(HERE)[1]
    for t in release.targets(reg):
        assert t.name in release.PREPARE, t.name


def test_run_release_collects_digests_and_stops_at_the_first_failure(
    tmp_path, monkeypatch
):
    fake = FakeDocker()
    ctx = make_ctx(tmp_path, fake)
    seen = []

    def fake_one(t, c):
        seen.append(t.key)
        if t.name == "ct-lesion-to-mni152":
            raise release.ReleaseError("boom")
        return {
            "step": t.key,
            "status": "published",
            "reference": f"{t.repository}@{D['a']}",
        }

    monkeypatch.setattr(release, "release_one", fake_one)
    with pytest.raises(release.ReleaseFailed) as info:
        release.run_release(None, ctx)
    assert seen == ["lesion-qc@1.0.0", "ct-lesion-to-mni152@1.0.0"]
    assert list(info.value.digests) == ["lesion-qc@1.0.0"]
    assert info.value.report[-1]["status"] == "failed"


def test_summary_marks_dry_runs():
    text = release.summary_markdown(
        [{"step": "a@1", "status": "dry-run", "image_id": D["a"]}], True
    )
    assert "DRY RUN" in text and "not pushed" in text


# --------------------------------------------------------------------------- pin and published


def test_committed_registry_is_unpublished_with_placeholders():
    reg = registry_tool.load(HERE)[1]
    assert reg["published"] is False
    assert registry_tool.PLACEHOLDER in json.dumps(reg)


def test_published_true_requires_real_digests_in_the_deployment_profile(tree):
    pipeline, reg = registry_tool.load(tree)
    reg["published"] = True
    errors = registry_tool.check(pipeline, reg)
    assert any("no real image digest" in e for e in errors)
    # the ci profile alone (stub) does not matter: pin only the deployment-enabled steps
    reg["published"] = False
    assert registry_tool.check(pipeline, reg) == []


def test_pin_writes_digests_keeps_comments_and_sets_published(tree):
    _, reg = registry_tool.load(tree)
    digests = all_digests(reg)
    before = (tree / "registry.yaml").read_text(encoding="utf-8")
    pinned = registry_tool.pin(tree, digests)
    assert sorted(pinned) == sorted(digests)
    pipeline, reg2 = registry_tool.load(tree)
    assert reg2["published"] is True
    assert registry_tool.PLACEHOLDER not in json.dumps(
        reg2
    ) and registry_tool.PLACEHOLDER not in json.dumps(pipeline)
    assert registry_tool.check(pipeline, reg2, registry_tool.load_presets(tree)) == []
    after = (tree / "registry.yaml").read_text(encoding="utf-8")
    assert "# kind: real" in after and after.count("\n") == before.count("\n")
    # materialize still takes the CI digests over the pinned ones
    ci = {k: v.replace(NS, "localhost:5000/ci") for k, v in digests.items()}
    new_pipeline, new_reg = registry_tool.materialize(pipeline, reg2, ci)
    assert all(
        e["image"].startswith("localhost:5000/")
        for impls in new_reg["implementations"].values()
        for e in impls.values()
    )


def test_pin_a_subset_leaves_the_rest_and_does_not_publish(tree):
    registry_tool.pin(tree, {"lesion-qc@1.0.0": digest_ref("lesion-qc")})
    _, reg = registry_tool.load(tree)
    assert reg["published"] is False
    assert reg["implementations"]["lesion-intake"]["lesion-qc"]["image"] == digest_ref(
        "lesion-qc"
    )
    assert (
        registry_tool.PLACEHOLDER
        in reg["implementations"]["tract-disconnection"]["lnm-tracts"]["image"]
    )


@pytest.mark.parametrize(
    "key, ref, message",
    [
        (
            "lesion-qc@1.0.0",
            "localhost:5000/ci/step-lesion-qc@" + "sha256:" + "a" * 64,
            "repository",
        ),
        ("lesion-qc@1.0.0", NS + "/step-lesion-qc:1.0.0", "digest-pinned"),
        ("lesion-qc@1.0.0", NS + "/step-lesion-qc@sha256:" + "0" * 64, "real digest"),
        ("nope@1.0.0", digest_ref("lesion-qc"), "not steps"),
    ],
)
def test_pin_refuses_bad_references_and_leaves_files_alone(tree, key, ref, message):
    before = {
        p.name: p.read_bytes() for p in (tree / "registry.yaml", tree / "pipeline.yaml")
    }
    with pytest.raises(ValueError, match=message):
        registry_tool.pin(tree, {key: ref})
    assert before == {
        p.name: p.read_bytes() for p in (tree / "registry.yaml", tree / "pipeline.yaml")
    }


def test_pin_refuses_build_local_entries(tree):
    text = (tree / "registry.yaml").read_text(encoding="utf-8")
    # an external, redistribution-forbidden wrapper: the only shape in which build: local is valid
    reg = registry_tool.load(tree)[1]
    entry = reg["implementations"]["functional-disconnection"]["flnm-fast"]
    entry["build"] = "local"
    entry["licence_review"]["image_redistribution"] = "forbidden"
    entry["image"] = "registry.internal.example/step-flnm-fast@sha256:" + "0" * 64
    pipeline = registry_tool.load(tree)[0]
    # the loaded documents are only used to prove the rule on `pin` through a patched load
    original = registry_tool.load
    registry_tool.load = lambda directory=HERE: (pipeline, reg)
    try:
        with pytest.raises(ValueError, match="build: local"):
            registry_tool.pin(tree, {"flnm-fast@2.0.0": digest_ref("flnm-fast")})
    finally:
        registry_tool.load = original
    assert (tree / "registry.yaml").read_text(encoding="utf-8") == text


def test_check_exempts_build_local_entries_from_published(tree):
    pipeline, reg = registry_tool.load(tree)
    reg = copy.deepcopy(reg)
    for impls in reg["implementations"].values():
        for entry in impls.values():
            if entry["kind"] != "planned":
                entry["image"] = entry["image"].replace("0" * 64, "a" * 64)
    reg["published"] = True
    flnm = reg["implementations"]["functional-disconnection"]["flnm-fast"]
    flnm["build"] = "local"
    flnm["licence_review"]["image_redistribution"] = "forbidden"
    flnm["image"] = "registry.internal.example/step-flnm-fast@sha256:" + "0" * 64
    assert not registry_tool.published_errors(reg)


def test_pin_cli(tree, tmp_path, capsys):
    _, reg = registry_tool.load(tree)
    digests = tmp_path / "digests.json"
    digests.write_text(json.dumps(all_digests(reg, "b")), encoding="utf-8")
    assert (
        registry_tool.main(["pin", "--digests", str(digests), "--dir", str(tree)]) == 0
    )
    out = capsys.readouterr().out
    assert "published: true" in out
    assert registry_tool.main(["check", "--dir", str(tree)]) == 0


def test_dest_override_redirects_the_publish_to_a_localhost_registry(
    tmp_path, lesion_qc
):
    fake = FakeDocker()
    ctx = make_ctx(tmp_path, fake)
    ctx.dest_override = "localhost:5001/rehearsal"
    entry = release.release_one(lesion_qc, ctx)
    assert entry["reference"] == f"localhost:5001/rehearsal/step-lesion-qc@{D['a']}"
    assert list(fake.remote) == ["localhost:5001/rehearsal/step-lesion-qc:1.0.0"]


def test_dest_override_is_refused_for_other_hosts(tmp_path):
    args = [
        "run", "--external", str(tmp_path), "--work", str(tmp_path / "w"),
        "--digests-out", str(tmp_path / "d.json"), "--report-out", str(tmp_path / "r.json"),
        "--dest-override", "registry.example.org/x", "--steps", "lesion-qc",
    ]  # fmt: skip
    assert release.main(args) == 1


def test_a_manifest_generator_runs_before_the_bundles_are_mounted(tmp_path):
    reg = registry_tool.load(HERE)[1]
    target = release.targets(reg, ["flnm-fast"])[0]
    fake = FakeDocker()
    ctx = make_ctx(tmp_path, fake)
    manifest = release.load_step_manifest(target, ctx)
    release.prepare_reference(target, ctx, manifest)
    scripts = [c for c in fake.calls if c[0] == sys.executable]
    assert scripts and scripts[0][1].replace(chr(92), "/").endswith(
        "tests/make_reference.py"
    )


def test_short_name_strips_docker_hub_hosts():
    assert (
        release._short_name("docker.io/compneurobilbaolab/step-x")
        == "compneurobilbaolab/step-x"
    )
    assert (
        release._short_name("index.docker.io/compneurobilbaolab/step-x")
        == "compneurobilbaolab/step-x"
    )
    assert release._short_name("localhost:5000/ci/step-x") == "localhost:5000/ci/step-x"
