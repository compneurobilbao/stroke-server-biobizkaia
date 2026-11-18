"""Registry rules for external implementations (card F1-23, run-plans.md 4.2 to 4.4).

`pipelines/stroke-lnm/registry.py check` must fail for an entry with ``origin: external``
that lacks the manifest's ``upstream`` block, a licence review, a validation status or
digest pins; ``build: local`` goes with a licence that forbids publishing the image; no
profile but ci enables an implementation below ``reference-tested``.
"""

from __future__ import annotations

import copy
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
HERE = REPO / "pipelines" / "stroke-lnm"
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location("stroke_lnm_registry_f123", HERE / "registry.py")
registry_tool = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = registry_tool
_spec.loader.exec_module(registry_tool)

SLOT, IMPL = "functional-disconnection", "flnm-fast"


@pytest.fixture()
def docs():
    return registry_tool.load(HERE)


@pytest.fixture()
def root(tmp_path):
    """A copy of the flnm-fast step (manifest and Dockerfile) that a test may damage."""
    step = tmp_path / "steps" / "flnm-fast"
    step.mkdir(parents=True)
    for name in ("step.yaml", "Dockerfile"):
        shutil.copy(REPO / "steps" / "flnm-fast" / name, step / name)
    return tmp_path


def errors_for(docs, root, mutate=None, *, manifest=None, dockerfile=None):
    pipeline, registry = copy.deepcopy(docs[0]), copy.deepcopy(docs[1])
    if mutate:
        mutate(registry["implementations"][SLOT][IMPL])
    if manifest:
        path = root / "steps" / "flnm-fast" / "step.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        manifest(data)
        path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    if dockerfile:
        path = root / "steps" / "flnm-fast" / "Dockerfile"
        path.write_text(dockerfile(path.read_text(encoding="utf-8")), encoding="utf-8")
    return registry_tool.check(pipeline, registry, root=root)


def test_committed_registry_passes_and_flnm_fast_is_the_worked_example(docs, root):
    assert errors_for(docs, root) == []
    entry = docs[1]["implementations"][SLOT][IMPL]
    assert entry["origin"] == "external"
    assert entry["validation"]["status"] == "reference-tested"
    assert entry["licence_review"]["image_redistribution"] == "allowed"
    assert entry["step"]["version"] == "1.1.0"
    # the repository's own manifest is the one the check read
    manifest = yaml.safe_load((REPO / "steps/flnm-fast/step.yaml").read_text(encoding="utf-8"))
    assert manifest["contract_version"] == "1.2"
    assert manifest["upstream"]["licence"] == entry["licence_review"]["code"]


def test_the_engine_defect_is_recorded_in_the_licence_notes(docs):
    notes = docs[1]["implementations"][SLOT][IMPL]["licence_review"]["notes"]
    assert "lowest" in notes and "lesion_single_parcel" in notes


def test_external_entry_without_upstream_fails(docs, root):
    found = errors_for(docs, root, manifest=lambda m: m.pop("upstream"))
    assert any("no upstream block" in e for e in found), found


def test_external_entry_with_an_older_contract_fails(docs, root):
    found = errors_for(docs, root, manifest=lambda m: m.update(contract_version="1.1"))
    assert any("contract 1.2" in e for e in found), found


def test_external_entry_without_licence_review_fails(docs, root):
    found = errors_for(docs, root, lambda e: e.pop("licence_review"))
    assert any("needs a licence_review" in e for e in found), found


@pytest.mark.parametrize("key", registry_tool.LICENCE_REVIEW_KEYS)
def test_every_licence_review_field_is_required(docs, root, key):
    found = errors_for(docs, root, lambda e: e["licence_review"].pop(key))
    assert any(f"licence_review.{key} is missing" in e for e in found), found


def test_licence_review_must_match_the_upstream_licence_and_have_a_date(docs, root):
    def damage(e):
        e["licence_review"]["code"] = "GPL-3.0-only"
        e["licence_review"]["reviewed"] = "yesterday"

    found = errors_for(docs, root, damage)
    assert any("differs from upstream.licence" in e for e in found), found
    assert any("YYYY-MM-DD" in e for e in found), found


def test_external_entry_without_validation_fails(docs, root):
    found = errors_for(docs, root, lambda e: e.pop("validation"))
    assert any("needs a validation status" in e for e in found), found


def test_validated_needs_a_record_file(docs, root):
    found = errors_for(docs, root, lambda e: e["validation"].update(status="validated"))
    assert any("validation.record" in e for e in found), found
    (root / "record.md").write_text("validation record", encoding="utf-8")
    found = errors_for(
        docs, root, lambda e: e["validation"].update(status="validated", record="record.md")
    )
    assert found == []


def test_unpinned_base_image_fails(docs, root):
    found = errors_for(
        docs,
        root,
        dockerfile=lambda t: t.replace(
            "FROM compneurobilbaolab/flnm-fast@sha256:", "FROM compneurobilbaolab/flnm-fast:latest\n#"
        ),
    )
    assert any("pinned by digest" in e for e in found), found


def test_upstream_image_must_be_the_dockerfile_base(docs, root):
    other = "0123456789abcdef" * 4
    found = errors_for(
        docs,
        root,
        manifest=lambda m: m["upstream"].update(
            image=f"docker.io/compneurobilbaolab/flnm-fast@sha256:{other}"
        ),
    )
    assert any("not the base image of the Dockerfile" in e for e in found), found


def test_upstream_image_tag_fails_the_schema_pin(docs, root):
    found = errors_for(
        docs,
        root,
        manifest=lambda m: m["upstream"].update(image="docker.io/compneurobilbaolab/flnm-fast:1.0"),
    )
    assert any("upstream.image is not pinned by digest" in e for e in found), found


def test_third_party_listing_must_match_the_upstream_block(docs, root):
    def drop(m):
        m["licence"]["third_party"] = [
            t for t in m["licence"]["third_party"] if not t["name"].startswith("flnm-fast")
        ]

    found = errors_for(docs, root, manifest=drop)
    assert any("licence.third_party" in e for e in found), found


def test_reference_tested_needs_a_reference_test(docs, root):
    found = errors_for(docs, root, manifest=lambda m: m.pop("reference_test"))
    assert any("reference_test" in e for e in found), found


def test_manifest_must_be_the_registered_step_version(docs, root):
    found = errors_for(docs, root, manifest=lambda m: m.update(version="1.0.0"))
    assert any("not the registry's step" in e for e in found), found


def test_build_local_goes_with_a_licence_that_forbids_publishing(docs, root):
    found = errors_for(docs, root, lambda e: e.update(build="local"))
    assert any("only for licence_review.image_redistribution: forbidden" in e for e in found)

    def forbid(e):
        e["licence_review"]["image_redistribution"] = "forbidden"

    found = errors_for(docs, root, forbid)
    assert any("must be build: local" in e for e in found), found

    def local(e):
        forbid(e)
        e["build"] = "local"

    assert errors_for(docs, root, local) == []


def test_build_local_is_for_external_entries(docs, root):
    pipeline, registry = copy.deepcopy(docs[0]), copy.deepcopy(docs[1])
    registry["implementations"]["lesion-intake"]["lesion-qc"]["build"] = "local"
    found = registry_tool.check(pipeline, registry, root=root)
    assert any("build: local is for origin: external" in e for e in found), found


def test_unknown_values_fail(docs, root):
    found = errors_for(
        docs, root, lambda e: e.update(origin="vendor", build="remote", validation={"status": "ok"})
    )
    assert any("unknown origin" in e for e in found), found
    assert any("unknown build" in e for e in found), found
    assert any("validation.status must be one of" in e for e in found), found


def test_manifest_of_a_fetched_external_source_needs_the_external_directory(docs, root):
    def move(e):
        e["source"] = {
            "repository": "example/tool",
            "commit": "0" * 40,
            "dockerfile": "step/Dockerfile",
        }

    found = errors_for(docs, root, move)
    assert any("pass --external" in e for e in found), found


def test_no_profile_but_ci_enables_below_reference_tested(docs, root):
    found = errors_for(docs, root, lambda e: e.pop("validation") and None)
    # an external entry without a status counts as unvalidated: the deployment profile refuses it
    assert any(
        "profile deployment: functional-disconnection/flnm-fast is unvalidated" in e
        and "implementation_not_validated" in e
        for e in found
    ), found
    assert not any("profile ci: functional-disconnection/flnm-fast is" in e for e in found)


def test_level_rule(docs):
    impls = docs[1]["implementations"]
    assert registry_tool.validation_level(impls[SLOT][IMPL]) == "reference-tested"
    assert registry_tool.validation_level(impls[SLOT]["fdisconnection-stub"]) == "unvalidated"
    assert registry_tool.validation_level(impls["lesion-intake"]["lesion-qc"]) == "validated"
    assert registry_tool.validation_level({"kind": "real", "origin": "external"}) == "unvalidated"
    assert registry_tool.validation_level({"kind": "planned"}) == "unvalidated"


def test_cli_check_fails_on_a_damaged_registry(tmp_path):
    import subprocess

    shutil.copytree(HERE, tmp_path / "stroke-lnm", ignore=shutil.ignore_patterns("tests", "__pycache__"))
    path = tmp_path / "stroke-lnm" / "registry.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    del data["implementations"][SLOT][IMPL]["licence_review"]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    done = subprocess.run(
        [sys.executable, str(HERE / "registry.py"), "check", "--dir", str(tmp_path / "stroke-lnm")],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 1
    assert "needs a licence_review" in done.stderr
