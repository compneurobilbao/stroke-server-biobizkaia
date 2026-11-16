"""Tests for the reference bundle registry (card F0-18)."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from contracts import validate as v
from orchestrator import bundles as b

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "data" / "bundles"
TOY_CONNECTOME = FIXTURES / "toy-normative-connectome" / "1.0.0"
TOY_TRACTS = FIXTURES / "toy-tract-atlas" / "1.0.0"


def make_bundle(
    dest: Path,
    version="1.0.0",
    bundle_id="synthetic-demo",
    btype="demo-type",
    payload=b"hello",
) -> Path:
    dest.mkdir(parents=True)
    (dest / "sub").mkdir()
    files = {"a.txt": payload, "sub/b.bin": bytes(range(50))}
    entries = []
    for rel, data in files.items():
        (dest / rel).write_bytes(data)
        entries.append(
            {
                "path": rel,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        )
    manifest = {
        "contract_version": "1.0",
        "kind": "Bundle",
        "id": bundle_id,
        "version": version,
        "type": btype,
        "title": "Synthetic demo",
        "synthetic": True,
        "licence": {"spdx": "CC0-1.0", "redistributable": True},
        "metadata": {"n": 2},
        "content_digest": v.bundle_digest(entries),
        "files": entries,
    }
    (dest / "bundle.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return dest


@pytest.fixture()
def reg(tmp_path):
    return b.BundleRegistry(tmp_path / "root")


@pytest.fixture()
def demo(tmp_path):
    return make_bundle(tmp_path / "src" / "demo")


def step(*reqs):
    return {"bundles": list(reqs)}


REQ = {"name": "demo", "type": "demo-type"}


def test_register_list_and_resolve(reg, demo):
    rec = reg.register(demo)
    assert (rec.bundle_id, rec.version, rec.verification_status) == (
        "synthetic-demo",
        "1.0.0",
        "verified",
    )
    assert rec.synthetic and rec.licence_spdx == "CC0-1.0" and rec.redistributable
    assert [r.bundle_id for r in reg.list()] == ["synthetic-demo"]
    assert reg.list(bundle_type="other") == []
    res = reg.resolve(step(REQ))
    assert res["demo"].host_path == reg.root / "synthetic-demo" / "1.0.0"
    assert res["demo"].mount_path == "/ref/demo"
    assert b.mount_specs(res) == [(str(res["demo"].host_path), "/ref/demo", "ro")]
    assert res["demo"].content_digest == rec.content_digest


def test_register_is_idempotent_and_versions_are_immutable(reg, demo, tmp_path):
    first = reg.register(demo)
    assert reg.register(demo).row_id == first.row_id
    other = make_bundle(tmp_path / "src" / "other", payload=b"different")
    with pytest.raises(b.BundleError, match="immutable"):
        reg.register(other)


def test_toy_fixture_bundles_register(reg):
    reg.register(TOY_CONNECTOME)
    reg.register(TOY_TRACTS)
    assert {r.bundle_type for r in reg.list()} == {
        "normative-connectome",
        "tract-atlas",
    }
    res = reg.resolve(
        step(
            {
                "name": "connectome",
                "type": "normative-connectome",
                "version": ">=1.0.0, <2.0.0",
            }
        )
    )
    assert res["connectome"].bundle_id == "toy-normative-connectome"


@pytest.mark.parametrize("tamper", ["content", "size", "missing", "extra"])
def test_tampered_source_is_not_registered(reg, demo, tamper):
    if tamper == "content":
        (demo / "a.txt").write_bytes(b"HELLO")  # same size, other hash
    elif tamper == "size":
        (demo / "a.txt").write_bytes(b"hello!")
    elif tamper == "missing":
        (demo / "sub" / "b.bin").unlink()
    else:
        (demo / "extra.txt").write_text("x")
    with pytest.raises(b.BundleVerificationError):
        reg.register(demo)
    assert reg.list() == []
    assert not (reg.root / "synthetic-demo").exists()


def test_manifest_edit_after_registration_is_rejected(reg, demo):
    reg.register(demo)
    stored = reg.root / "synthetic-demo" / "1.0.0" / "bundle.yaml"
    stored.write_text(stored.read_text().replace("Synthetic demo", "Edited title"))
    with pytest.raises(b.BundleVerificationError, match="changed since registration"):
        reg.resolve(step(REQ))


def test_digest_not_matching_files_is_rejected(reg, demo):
    path = demo / "bundle.yaml"
    data = yaml.safe_load(path.read_text())
    data["content_digest"] = "sha256:" + "0" * 64
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(b.BundleVerificationError, match="content_digest"):
        reg.register(demo)


def test_missing_licence_is_rejected(reg, demo):
    path = demo / "bundle.yaml"
    data = yaml.safe_load(path.read_text())
    del data["licence"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(b.BundleVerificationError, match="licence"):
        reg.register(demo)


def test_tamper_after_registration_refuses_resolution(reg, demo):
    reg.register(demo)
    stored = reg.root / "synthetic-demo" / "1.0.0"
    (stored / "a.txt").write_bytes(b"HELLO")
    # The cheap pre-mount check only looks at sizes; same-size tampering needs full.
    reg.resolve(step(REQ))
    with pytest.raises(b.BundleVerificationError, match="SHA-256"):
        reg.resolve(step(REQ), full=True)
    # The failure is recorded; later cheap resolution refuses too.
    assert reg.list()[0].verification_status == "failed"
    with pytest.raises(b.BundleVerificationError, match="failed verification"):
        reg.resolve(step(REQ))


def test_size_change_caught_by_quick_check(reg, demo):
    reg.register(demo)
    (reg.root / "synthetic-demo" / "1.0.0" / "a.txt").write_bytes(b"longer content")
    with pytest.raises(b.BundleVerificationError, match="size"):
        reg.resolve(step(REQ))


def test_missing_file_refuses_resolution(reg, demo):
    reg.register(demo)
    (reg.root / "synthetic-demo" / "1.0.0" / "sub" / "b.bin").unlink()
    with pytest.raises(b.BundleVerificationError):
        reg.resolve(step(REQ))


def test_verify_reports_and_recovers(reg, demo):
    reg.register(demo)
    stored = reg.root / "synthetic-demo" / "1.0.0" / "a.txt"
    stored.write_bytes(b"HELLO")
    with pytest.raises(b.BundleVerificationError):
        reg.verify("synthetic-demo", "1.0.0")
    stored.write_bytes(b"hello")  # e.g. re-copied from the source
    assert reg.verify("synthetic-demo", "1.0.0").verification_status == "verified"
    assert [e for _, e in reg.verify_all()] == [None]


def test_missing_required_bundle_is_refused(reg, demo):
    with pytest.raises(b.BundleNotFoundError, match="demo"):
        reg.resolve(step(REQ))
    reg.register(demo)
    with pytest.raises(b.BundleNotFoundError):
        reg.resolve(step({"name": "x", "type": "demo-type", "id": "other-id"}))
    with pytest.raises(b.BundleNotFoundError):
        reg.resolve(step({"name": "x", "type": "demo-type", "version": ">=2.0.0"}))
    # optional requirements are skipped
    assert reg.resolve(step({"name": "x", "type": "nothing", "required": False})) == {}


def test_withdrawn_bundle_not_resolved(reg, demo):
    reg.register(demo)
    reg.withdraw("synthetic-demo", "1.0.0")
    with pytest.raises(b.BundleNotFoundError):
        reg.resolve(step(REQ))


def test_highest_matching_version_and_pinning(reg, tmp_path):
    for ver in ("1.0.0", "1.2.0", "2.0.0"):
        reg.register(make_bundle(tmp_path / ver, version=ver, payload=ver.encode()))
    r = {"name": "demo", "type": "demo-type", "version": ">=1.0.0, <2.0.0"}
    assert reg.resolve(step(r))["demo"].version == "1.2.0"
    assert reg.resolve(step(REQ))["demo"].version == "2.0.0"
    pinned = reg.resolve(step(r), selection={"demo": ("synthetic-demo", "1.0.0")})
    assert pinned["demo"].version == "1.0.0"
    with pytest.raises(b.BundleNotFoundError):  # pin outside the required range
        reg.resolve(step(r), selection={"demo": ("synthetic-demo", "2.0.0")})


def test_json_store_persists(tmp_path, demo):
    root = tmp_path / "root"
    b.BundleRegistry(root).register(demo)
    assert len(b.BundleRegistry(root).list()) == 1


def test_sqlalchemy_store(tmp_path, demo):
    from sqlalchemy.orm import sessionmaker

    from app.models import reference_bundle  # noqa: F401
    from tests import pgdb

    engine = pgdb.new_engine(tmp_path, "db.sqlite")
    with sessionmaker(bind=engine)() as session:
        reg = b.BundleRegistry(tmp_path / "root", b.SqlAlchemyStore(session))
        rec = reg.register(demo)
        assert rec.bundle_id == "synthetic-demo"
        assert reg.register(demo).row_id == rec.row_id
        assert reg.resolve(step(REQ))["demo"].version == "1.0.0"
        (reg.root / "synthetic-demo" / "1.0.0" / "a.txt").write_bytes(b"HELLO")
        with pytest.raises(b.BundleVerificationError):
            reg.verify("synthetic-demo", "1.0.0")
        assert reg.list()[0].verification_status == "failed"
        reg.withdraw("synthetic-demo", "1.0.0")
        assert reg.list(status="withdrawn")[0].withdrawn_at is not None


def test_cli_roundtrip(tmp_path, demo):
    script = ROOT / "scripts" / "bundles" / "registry.py"
    root = tmp_path / "root"

    def run(*args):
        return subprocess.run(
            [sys.executable, str(script), "--root", str(root), *args],
            capture_output=True,
            text=True,
        )

    assert run("register", str(demo)).returncode == 0
    out = run("list")
    assert out.returncode == 0 and "synthetic-demo" in out.stdout
    assert run("verify").returncode == 0
    tampered = tmp_path / "t"
    shutil.copytree(demo, tampered)
    (tampered / "a.txt").write_bytes(b"HELLO")
    assert run("register", str(tampered)).returncode == 1
    (root / "synthetic-demo" / "1.0.0" / "a.txt").write_bytes(b"HELLO")
    bad = run("verify")
    assert bad.returncode == 1 and "FAILED" in bad.stdout
    step_yaml = tmp_path / "step.yaml"
    step_yaml.write_text(yaml.safe_dump(step(REQ)))
    res = run("resolve", str(step_yaml))
    assert res.returncode == 1 and "error:" in res.stderr
