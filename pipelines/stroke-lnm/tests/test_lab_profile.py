"""The minimum pipeline of the lab (card MVP-01), offline.

The `deployment` profile of ``registry.yaml`` enables only what the lab runs today: a lesion
mask already in MNI152 space goes through intake and QC, functional disconnection (flnm-fast,
HCP cohorts) with the COGNET-STROKE profile, and tract-wise disconnection, into the report.
Normalisation of CT lesions stays in the `ci` profile only. The other stages of the process
are listed as in development (display only). Published step digests are pinned. Everything
here reads the repository's own documents; no data of any person is involved.
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location("stroke_lnm_registry_lab", HERE / "registry.py")
registry_tool = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = registry_tool
_spec.loader.exec_module(registry_tool)

from orchestrator.catalog import (  # noqa: E402
    DirectoryPipelineRegistry,
    DirectoryPresetRegistry,
    StaticStepCatalog,
)
from orchestrator.planning import (  # noqa: E402
    Enablement,
    Plan,
    PlanRefused,
    PlanRequest,
    plan,
)

PRESETS = ["full", "mni-mask-analysis", "ct-normalisation", "lesion-qc-report"]
CT, MNI = "ct-and-native-mask", "mni-mask"
VERSION = "0.11.0"
MINIMUM_SLOTS = {
    "lesion-intake",
    "functional-disconnection",
    "cognet-profile",
    "tract-disconnection",
    "report",
}

# Digests of the Docker Hub release runs 45356289386 (all steps), 45359587628 (flnm-fast
# 3.0.0; flnm-fast 2.0.0 of the first run is superseded), 45404842617 (research-report 0.4.0)
# and 45628376765 (cognet-stroke 1.1.0, ct-lesion-to-mni152 1.0.1; lnm-tracts 1.0.1 of that run
# is superseded by 1.1.0).
PUBLISHED = {
    "cognet-stroke@1.1.0": "docker.io/compneurobilbaolab/step-cognet-stroke@sha256:c6cf1186311ea65237d8235a95e9a02c314acba44e03f23844b472592cc88d42",
    "ct-lesion-to-mni152@1.0.1": "docker.io/compneurobilbaolab/step-ct-lesion-to-mni152@sha256:717b4ab02c9d8fab0f1ccf4de16d3a764b17960b9525b9fc418e2485ceb86b1d",
    "fdisconnection-stub@0.1.0": "docker.io/compneurobilbaolab/step-fdisconnection-stub@sha256:8da4c433c7c9f6cf0dc9f59536d40f310fc8f09ff0d0c2dd28715a5e70c55745",
    "flnm-fast@3.0.0": "docker.io/compneurobilbaolab/step-flnm-fast@sha256:368df2c450b30f02d43d30bdf8ec9750bcf43f6b208731dde64d30bbf3c5efb0",
    "lesion-qc@1.0.0": "docker.io/compneurobilbaolab/step-lesion-qc@sha256:c23607186e6c771164ca6f0b8dcdd0509ec895344602cb60d9c4b0d841dcaf32",
    "research-report@0.4.0": "docker.io/compneurobilbaolab/step-research-report@sha256:1f700ac224d4bd2e58a0c77201573bbe8d4ad7d6607cd6c271ae6be1796ff537",
}
# Step versions added by a later change than the release runs above: placeholder digest (and
# `published: false`) until released and pinned with `registry.py pin`; any real digest after.
# STEPS-02: lnm-tracts 1.1.0 (lesion resampled into the space of the tract atlas). The 1.0.1
# image (760cccde...) stays on Docker Hub; CI compares the new image with it.
# REPORT-02: research-report 0.5.0 (synthetic example input stated on the first page); the 0.4.0
# image stays on Docker Hub.
PENDING_RELEASE = {"lnm-tracts@1.1.0", "research-report@0.5.0"}


@pytest.fixture(scope="module")
def documents():
    return registry_tool.load(HERE)


@pytest.fixture(scope="module")
def pipeline():
    found = DirectoryPipelineRegistry(HERE).get("stroke-lnm", VERSION)
    assert found is not None
    return found


@pytest.fixture(scope="module")
def presets():
    return DirectoryPresetRegistry(HERE, DirectoryPipelineRegistry(HERE), StaticStepCatalog())


def enablement(registry: dict, profile: str) -> Enablement:
    rows = [SimpleNamespace(**r) for r in registry_tool.enabled_settings(registry, profile)]
    settings = registry_tool.enabled_preset_settings(registry, profile, PRESETS)
    return Enablement.from_settings(rows, [SimpleNamespace(**r) for r in settings])


# ------------------------------------------------------------------ the deployment profile


def test_deployment_enables_exactly_the_minimum_implementations(documents):
    _pipeline, registry = documents
    spec = registry["profiles"]["deployment"]
    assert spec["enabled"] == {
        "lesion-intake": ["lesion-qc"],
        "functional-disconnection": ["flnm-fast-hcp-aging", "flnm-fast-hcp-ya"],
        "tract-disconnection": ["lnm-tracts-hcp1065"],
        "cognet-profile": ["cognet-stroke-concept-maps"],
        "report": ["research-report", "research-report-es"],
    }
    assert spec["default"] == {
        "lesion-intake": "lesion-qc",
        "functional-disconnection": "flnm-fast-hcp-aging",
        "tract-disconnection": "lnm-tracts-hcp1065",
        "cognet-profile": "cognet-stroke-concept-maps",
        "report": "research-report",
    }
    assert spec["presets"] == {"enabled": ["mni-mask-analysis"], "default": "mni-mask-analysis"}


def test_normalisation_stays_in_the_ci_profile_only(documents):
    _pipeline, registry = documents
    assert "normalisation" not in registry["profiles"]["deployment"]["enabled"]
    assert registry["profiles"]["ci"]["enabled"]["normalisation"] == ["ct-lesion-to-mni152"]
    rows = {
        (r["slot"], r["implementation_id"]): r for r in registry_tool.enabled_settings(registry, "deployment")
    }
    assert rows[("normalisation", "ct-lesion-to-mni152")] == {
        "slot": "normalisation",
        "implementation_id": "ct-lesion-to-mni152",
        "enabled": False,
        "is_default": False,
    }


def test_the_deployment_profile_enables_no_stub_and_no_ci_fixture(documents):
    _pipeline, registry = documents
    for slot, ids in registry["profiles"]["deployment"]["enabled"].items():
        for impl_id in ids:
            assert registry["implementations"][slot][impl_id]["kind"] == "real"
    assert not any("synthetic" in i for ids in registry["profiles"]["deployment"]["enabled"].values() for i in ids)


def test_minimum_preset_contains_exactly_the_minimum(presets):
    preset = presets.get("stroke-lnm", VERSION, "mni-mask-analysis")
    assert preset is not None
    assert preset.input_sets == ["mni-mask"]
    # Intake and QC is reached upstream (the report consumes lesion_qc); everything else is named.
    assert set(preset.required) == {"functional-disconnection", "cognet-profile", "tract-disconnection", "report"}
    assert list(preset.if_available) == []


def test_minimum_preset_plans_exactly_the_minimum_with_the_lab_implementations(documents, pipeline, presets):
    result = plan(
        pipeline,
        MNI,
        PlanRequest(preset=presets.get("stroke-lnm", VERSION, "mni-mask-analysis")),
        enablement(documents[1], "deployment"),
    )
    assert isinstance(result, Plan), getattr(result, "reasons", result)
    assert {s.slot for s in result.selected} == MINIMUM_SLOTS
    chosen = {s.slot: s.implementation_id for s in result.selected}
    assert chosen == {
        "lesion-intake": "lesion-qc",
        "functional-disconnection": "flnm-fast-hcp-aging",
        "tract-disconnection": "lnm-tracts-hcp1065",
        "cognet-profile": "cognet-stroke-concept-maps",
        "report": "research-report",
    }
    assert [(s.slot, s.reason) for s in result.slots if s.status != "selected"] == [
        ("normalisation", "condition_false")
    ]


def test_ct_runs_are_refused_while_normalisation_is_unmounted(documents, pipeline, presets):
    refused = plan(pipeline, CT, PlanRequest(), enablement(documents[1], "deployment"))
    assert isinstance(refused, PlanRefused)
    assert [(r.code, r.slot) for r in refused.reasons] == [("no_implementation", "normalisation")]


def test_other_presets_are_not_offered_in_the_deployment_profile(documents, pipeline, presets):
    en = enablement(documents[1], "deployment")
    for name in ("full", "ct-normalisation", "lesion-qc-report"):
        refused = plan(pipeline, MNI, PlanRequest(preset=presets.get("stroke-lnm", VERSION, name)), en)
        assert isinstance(refused, PlanRefused) and refused.code == "preset_not_enabled", name


# ------------------------------------------------------------------------ bundle pins


def _allowed(pipeline_doc: dict, slot: str) -> dict:
    (found,) = [s for s in pipeline_doc["slots"] if s["name"] == slot]
    return {a["id"]: a for a in found["implementations"]["allowed"]}


def test_deployment_entries_pin_the_lab_bundles_and_ci_entries_stay_unpinned(documents):
    pipeline_doc, registry = documents
    tracts = _allowed(pipeline_doc, "tract-disconnection")
    cognet = _allowed(pipeline_doc, "cognet-profile")
    # The HCP1065 atlas is in MNI152NLin2009aAsym (hcp1065-tracts 1.1.0 declares it), so the entry
    # also pins the transform from the lesion space (MNI152NLin6Asym) into that space.
    assert tracts["lnm-tracts-hcp1065"]["bundles"] == {
        "tract_atlas": {"id": "hcp1065-tracts", "version": "1.1.0"},
        "space_transform": {"id": "xfm-nlin6asym-to-nlin2009aasym", "version": "1.0.0"},
    }
    assert cognet["cognet-stroke-concept-maps"]["bundles"] == {
        "concept_maps": {"id": "cognet-concept-maps", "version": "1.0.0"}
    }
    # CI keeps the unpinned entries, so it resolves the synthetic bundles it registers.
    assert "bundles" not in tracts["lnm-tracts"] and "bundles" not in cognet["cognet-stroke"]
    ci = registry["profiles"]["ci"]
    assert ci["enabled"]["tract-disconnection"] == ["lnm-tracts"]
    assert ci["enabled"]["cognet-profile"] == ["cognet-stroke"]
    # Same step, same image: only the pin differs.
    assert tracts["lnm-tracts"]["step"] == tracts["lnm-tracts-hcp1065"]["step"]
    assert tracts["lnm-tracts"]["image"] == tracts["lnm-tracts-hcp1065"]["image"]
    assert cognet["cognet-stroke"]["image"] == cognet["cognet-stroke-concept-maps"]["image"]
    flnm = _allowed(pipeline_doc, "functional-disconnection")
    assert flnm["flnm-fast-hcp-aging"]["bundles"]["normative"] == {"id": "normative-fc-hcp-aging", "version": "1.0.0"}
    assert flnm["flnm-fast-hcp-ya"]["bundles"]["normative"] == {"id": "normative-fc-hcp-ya", "version": "1.0.0"}


# ------------------------------------------------------------------------ real digests


def test_every_step_is_pinned_to_its_published_digest(documents):
    pipeline_doc, registry = documents
    for slot in pipeline_doc["slots"]:
        for entry in slot["implementations"]["allowed"]:
            key = f"{entry['step']['name']}@{entry['step']['version']}"
            if key in PENDING_RELEASE:
                assert registry_tool.DIGEST_REF.match(entry["image"]), (slot["name"], entry["id"])
            else:
                assert entry["image"] == PUBLISHED[key], (slot["name"], entry["id"])
            assert entry["image"] == registry["implementations"][slot["name"]][entry["id"]]["image"]
    # `published` is true exactly when no placeholder is left
    pending = any(
        registry_tool.PLACEHOLDER in (HERE / name).read_text(encoding="utf-8")
        for name in ("pipeline.yaml", "registry.yaml")
    )
    assert registry["published"] is (not pending)


def test_registry_check_passes_and_requires_real_digests_once_published(documents):
    pipeline_doc, registry = documents
    assert registry_tool.check(pipeline_doc, registry, registry_tool.load_presets(HERE)) == []
    broken = copy.deepcopy(registry)
    broken["published"] = True
    entry = broken["implementations"]["tract-disconnection"]["lnm-tracts-hcp1065"]
    entry["image"] = "docker.io/compneurobilbaolab/step-lnm-tracts@sha256:" + "0" * 64
    errors = registry_tool.published_errors(broken)
    assert any("tract-disconnection/lnm-tracts-hcp1065" in e for e in errors)


def test_materialize_still_replaces_every_digest_for_ci(documents):
    pipeline_doc, registry = documents
    keys = {
        f"{e['step']['name']}@{e['step']['version']}"
        for slot in pipeline_doc["slots"]
        for e in slot["implementations"]["allowed"]
    }
    digests = {key: f"registry.example/{key.split('@')[0]}@sha256:" + "b" * 64 for key in keys}
    out_pipeline, out_registry = registry_tool.materialize(pipeline_doc, registry, digests)
    for slot in out_pipeline["slots"]:
        for entry in slot["implementations"]["allowed"]:
            assert entry["image"].startswith("registry.example/")
    assert out_registry["in_development"] == registry["in_development"]
    assert out_registry["profiles"] == registry["profiles"]


# ------------------------------------------------------------- titles and in-development


def test_the_two_hcp_cohort_entries_have_different_titles(documents):
    _pipeline, registry = documents
    flnm = registry["implementations"]["functional-disconnection"]
    assert flnm["flnm-fast-hcp-aging"]["title"] == "flnm-fast · HCP Aging"
    assert flnm["flnm-fast-hcp-ya"]["title"] == "flnm-fast · HCP Young Adult"
    assert flnm["flnm-fast-synthetic"]["title"] != flnm["flnm-fast-hcp-aging"]["title"]
    assert registry["implementations"]["tract-disconnection"]["lnm-tracts-hcp1065"]["title"] != (
        registry["implementations"]["tract-disconnection"]["lnm-tracts"]["title"]
    )


def test_the_pipeline_file_has_no_title_on_implementation_entries(documents):
    """The pipeline schema (contract 1.2) has no `title` on an implementation: titles live in the registry."""
    pipeline_doc, _registry = documents
    for slot in pipeline_doc["slots"]:
        for entry in slot["implementations"]["allowed"]:
            assert "title" not in entry


def test_in_development_lists_the_stages_that_cannot_be_used_yet(documents):
    _pipeline, registry = documents
    stages = registry["in_development"]
    assert [s["id"] for s in stages] == ["normalisation", "segmentation", "lesion-bank", "voxel-lnm"]
    assert stages[0] == {"id": "normalisation", "slot": "normalisation"}
    assert all(s["title"] for s in stages[1:])


def test_a_stage_in_development_is_never_enabled_by_a_profile(documents):
    _pipeline, registry = documents
    ids = {s["id"] for s in registry["in_development"] if "slot" not in s}
    for profile in registry["profiles"].values():
        for slot, impls in profile["enabled"].items():
            assert slot not in ids
            assert not ids & set(impls)
    # Seeding writes one row per implementation, and a stage without a slot has none.
    rows = registry_tool.enabled_settings(registry, "deployment")
    assert not ids & {r["slot"] for r in rows}
    assert not ids & {r["implementation_id"] for r in rows}


def _errors(edit) -> list[str]:
    pipeline_doc, registry = copy.deepcopy(registry_tool.load(HERE))
    edit(registry)
    return registry_tool.display_errors(registry, {s["name"] for s in pipeline_doc["slots"]})


def test_the_registry_check_validates_the_display_fields(documents):
    assert _errors(lambda r: None) == []
    assert any("not a slot" in e for e in _errors(lambda r: r["in_development"].append({"id": "x", "slot": "nope"})))
    assert any("needs a title" in e for e in _errors(lambda r: r["in_development"].append({"id": "future-stage"})))
    assert any("twice" in e for e in _errors(lambda r: r["in_development"].append({"id": "segmentation", "title": "Again"})))
    assert any("kebab" in e for e in _errors(lambda r: r["in_development"].append({"id": "Bad Id", "title": "x"})))
    assert any("slot name" in e for e in _errors(lambda r: r["in_development"].append({"id": "report", "title": "x"})))
    assert any("unknown keys" in e for e in _errors(lambda r: r["in_development"].append({"id": "x-y", "title": "t", "enabled": True})))
    assert any(
        "non-empty" in e
        for e in _errors(lambda r: r["implementations"]["report"]["research-report"].update(title=" "))
    )
    assert any("must be a list" in e for e in _errors(lambda r: r.update(in_development={"id": "x"})))


def test_the_registry_without_display_fields_is_still_valid(documents):
    pipeline_doc, registry = copy.deepcopy(documents)
    registry.pop("in_development")
    for impls in registry["implementations"].values():
        for entry in impls.values():
            entry.pop("title", None)
    assert registry_tool.check(pipeline_doc, registry, registry_tool.load_presets(HERE)) == []


def test_registry_files_are_utf8_yaml_documents():
    for name in ("registry.yaml", "pipeline.yaml"):
        assert isinstance(yaml.safe_load((HERE / name).read_text(encoding="utf-8")), dict)
