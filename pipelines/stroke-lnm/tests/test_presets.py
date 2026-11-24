"""The four stroke-lnm presets (card F1-20), offline.

The presets in ``pipelines/stroke-lnm/presets/`` are validated against the committed
pipeline (contract rules P1-P8 through ``registry.py`` and the orchestrator's
``DirectoryPresetRegistry``), planned for both input sets with the deployment and ci
profiles of ``registry.yaml`` (``orchestrator.planning.plan``), and the plan of ``full`` is
compared with the full plan (rule R0). Everything here is the repository's own documents;
no data of any person is involved. The runs through the orchestrator are in
``test_e2e_stroke_lnm.py``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_spec = importlib.util.spec_from_file_location("stroke_lnm_registry_presets", HERE / "registry.py")
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
    provenance_consumer_reasons,
)

PRESETS = ["full", "mni-mask-analysis", "ct-normalisation", "lesion-qc-report"]
CT, MNI = "ct-and-native-mask", "mni-mask"
VERSION = "0.8.0"
ALL_SLOTS = [
    "lesion-intake",
    "normalisation",
    "functional-disconnection",
    "tract-disconnection",
    "cognet-profile",
    "report",
]


@pytest.fixture(scope="module")
def documents():
    return registry_tool.load(HERE)


@pytest.fixture(scope="module")
def pipeline():
    found = DirectoryPipelineRegistry(HERE).get("stroke-lnm", VERSION)
    assert found is not None
    return found


@pytest.fixture(scope="module")
def presets(pipeline):
    return DirectoryPresetRegistry(HERE, DirectoryPipelineRegistry(HERE))


def enablement(registry: dict, profile: str, unmounted: tuple[str, ...] = ()) -> Enablement:
    """What a deployment with ``profile`` enables (its seeded rows), minus ``unmounted`` slots."""
    rows = [
        SimpleNamespace(**r)
        for r in registry_tool.enabled_settings(registry, profile)
        if r["slot"] not in unmounted
    ]
    settings = registry_tool.enabled_preset_settings(registry, profile, PRESETS)
    return Enablement.from_settings(rows, [SimpleNamespace(**r) for r in settings])


def statuses(result) -> dict[str, tuple[str, str | None]]:
    assert isinstance(result, Plan), getattr(result, "reasons", result)
    return {s.slot: (s.status, s.reason) for s in result.slots}


# ------------------------------------------------------------------------------ validity


def test_the_four_presets_are_the_files_of_the_directory():
    assert sorted(p.stem for p in (HERE / "presets").glob("*.yaml")) == sorted(PRESETS)


def test_registry_check_validates_the_presets(documents):
    pipeline, registry = documents
    assert registry_tool.check(pipeline, registry, registry_tool.load_presets(HERE)) == []


def test_presets_are_attached_to_the_pipeline_version(presets):
    """P1-P8 through the orchestrator's own loader: all four are offered for 0.8.0."""
    assert [p.id for p in presets.presets("stroke-lnm", VERSION)] == sorted(PRESETS)
    for preset in presets.presets("stroke-lnm", VERSION):
        assert preset.version == "1.6.0"
        assert preset.sha256 == hashlib.sha256((HERE / "presets" / f"{preset.id}.yaml").read_bytes()).hexdigest()


def test_the_presets_are_written_for_the_current_pipeline_range():
    for name in PRESETS:
        data = yaml.safe_load((HERE / "presets" / f"{name}.yaml").read_text(encoding="utf-8"))
        assert data["pipeline"] == {"id": "stroke-lnm", "versions": ">=0.2.0, <0.9.0"}


# What each profile offers: the ci profile every preset (default `full`); the deployment (lab)
# profile only the minimum preset, because the others also accept the CT input set, which needs
# the normalisation slot that the lab does not mount yet (MVP-01).
PROFILE_PRESETS = {"ci": (set(PRESETS), "full"), "deployment": ({"mni-mask-analysis"}, "mni-mask-analysis")}


@pytest.mark.parametrize("profile", sorted(PROFILE_PRESETS))
def test_the_profiles_enable_their_presets_with_one_default(documents, profile):
    _pipeline, registry = documents
    enabled, default = PROFILE_PRESETS[profile]
    rows = registry_tool.enabled_preset_settings(registry, profile, PRESETS)
    assert {r["preset_id"] for r in rows if r["enabled"]} == enabled
    assert [r["preset_id"] for r in rows if r["is_default"]] == [default]
    # without the list of the pipeline's presets: the enabled ones
    assert registry_tool.enabled_preset_settings(registry, profile) == [r for r in rows if r["enabled"]]


def test_an_unlisted_preset_is_a_disabled_row(documents):
    _pipeline, registry = documents
    rows = registry_tool.enabled_preset_settings(registry, "ci", [*PRESETS, "other"])
    assert rows[-1] == {"preset_id": "other", "enabled": False, "is_default": False}


# --------------------------------------------------------------------- registry check


def _check(documents, edit_registry=None, edit_presets=None) -> list[str]:
    import copy

    pipeline, registry = copy.deepcopy(documents[0]), copy.deepcopy(documents[1])
    presets = {k: (copy.deepcopy(d), raw) for k, (d, raw) in registry_tool.load_presets(HERE).items()}
    if edit_registry:
        edit_registry(registry)
    if edit_presets:
        edit_presets(presets)
    return registry_tool.check(pipeline, registry, presets)


def test_check_refuses_a_profile_that_enables_a_missing_preset(documents):
    errors = _check(documents, lambda r: r["profiles"]["ci"]["presets"]["enabled"].append("nope"))
    assert any("enabled preset 'nope' has no file" in e for e in errors)


def test_check_refuses_a_default_that_is_not_enabled(documents):
    def edit(registry):
        registry["profiles"]["deployment"]["presets"] = {"enabled": ["full"], "default": "lesion-qc-report"}

    assert any("default preset 'lesion-qc-report' is not enabled" in e for e in _check(documents, edit))


def test_check_refuses_a_preset_whose_range_excludes_the_pipeline(documents):
    def edit(presets):
        presets["full"][0]["pipeline"]["versions"] = ">=0.1.0, <0.2.0"

    assert any("(P2)" in e for e in _check(documents, edit_presets=edit))


def test_check_refuses_an_infeasible_or_unknown_target(documents):
    def edit(presets):
        presets["full"][0]["targets"]["required"].append("not-a-slot")

    assert any("[unknown_target]" in e for e in _check(documents, edit_presets=edit))


def test_check_refuses_a_preset_that_does_not_apply_to_its_input_set(documents):
    def edit(presets):
        presets["mni-mask-analysis"][0]["targets"]["required"].append("normalisation")

    assert any("normalisation" in e for e in _check(documents, edit_presets=edit))


def test_check_refuses_an_id_that_differs_from_the_file_name(documents):
    def edit(presets):
        presets["full"][0]["id"] = "everything"

    assert any("must equal the file name" in e for e in _check(documents, edit_presets=edit))


def test_check_refuses_contract_1_1(documents):
    def edit(presets):
        presets["full"][0]["contract_version"] = "1.1"

    assert any("P1" in e for e in _check(documents, edit_presets=edit))


def test_check_refuses_a_target_that_is_required_and_if_available(documents):
    def edit(presets):
        presets["full"][0]["targets"]["if_available"].append("report")

    assert any("P7" in e for e in _check(documents, edit_presets=edit))


def test_check_without_presets_keeps_the_earlier_behaviour(documents):
    assert registry_tool.check(*documents) == []


# ----------------------------------------------------------------------- full == R0

# Enablements under which the full plan and the preset must agree: every slot mounted, and
# the optional slots left out. Tract disconnection is not among them on purpose: the preset
# names it as required, the full plan treats it as optional (see the test below).
VARIANTS = [(), ("functional-disconnection",), ("cognet-profile",), ("functional-disconnection", "cognet-profile")]


# ci only: the deployment (lab) profile enables just the minimum preset and mounts no normalisation.
@pytest.mark.parametrize(("profile", "input_set"), [("ci", CT), ("ci", MNI)])
@pytest.mark.parametrize("unmounted", VARIANTS, ids=lambda v: "+".join(v) or "all-mounted")
def test_full_preset_gives_the_full_plan(documents, pipeline, presets, profile, unmounted, input_set):
    """Same slots, statuses, reasons and implementations as the full plan (rule R0); only the
    plan role differs (slots reached from the targets are `upstream`)."""
    en = enablement(documents[1], profile, unmounted)
    full_plan = plan(pipeline, input_set, PlanRequest(), en)
    preset_plan = plan(
        pipeline, input_set, PlanRequest(preset=presets.get("stroke-lnm", VERSION, "full")), en
    )
    assert isinstance(full_plan, Plan) and isinstance(preset_plan, Plan)
    assert [s.slot for s in preset_plan.slots] == ALL_SLOTS

    def key(s):
        return (s.slot, s.status, s.reason, s.implementation_id, s.chosen_by)

    assert [key(s) for s in preset_plan.slots] == [key(s) for s in full_plan.slots]
    assert preset_plan.mode == "preset" and full_plan.mode == "full"
    assert full_plan.targets == {}


def test_full_names_tract_disconnection_as_required(documents, pipeline, presets):
    """The one difference from the full plan: without an enabled tract implementation the
    full plan skips the optional slot (QC-only report), the preset refuses the run."""
    en = enablement(documents[1], "ci", ("tract-disconnection",))
    assert statuses(plan(pipeline, MNI, PlanRequest(), en))["tract-disconnection"] == (
        "unavailable",
        "no_implementation",
    )
    refused = plan(pipeline, MNI, PlanRequest(preset=presets.get("stroke-lnm", VERSION, "full")), en)
    assert isinstance(refused, PlanRefused)
    assert [(r.code, r.slot) for r in refused.reasons] == [("no_implementation", "tract-disconnection")]


# ------------------------------------------------------------------ the design table

PLANNED = ("selected", None)
NOT_REQUESTED = ("skipped", "not_requested")
CONDITION_FALSE = ("skipped", "condition_false")

# docs/design/run-plans.md 2.5 for stroke-lnm 0.2.0, every slot mounted: (preset, input set)
# -> {slot: (status, reason)} and {slot: plan role} for the slots that are in the plan.
EXPECTED = {
    ("full", CT): (
        dict.fromkeys(ALL_SLOTS, PLANNED),
        {
            "lesion-intake": "upstream",
            "normalisation": "upstream",
            "functional-disconnection": "if_available",
            "tract-disconnection": "target",
            "cognet-profile": "if_available",
            "report": "target",
        },
    ),
    ("full", MNI): (
        {**dict.fromkeys(ALL_SLOTS, PLANNED), "normalisation": CONDITION_FALSE},
        {
            "lesion-intake": "upstream",
            "functional-disconnection": "if_available",
            "tract-disconnection": "target",
            "cognet-profile": "if_available",
            "report": "target",
        },
    ),
    ("mni-mask-analysis", MNI): (
        {**dict.fromkeys(ALL_SLOTS, PLANNED), "normalisation": CONDITION_FALSE},
        {
            "lesion-intake": "upstream",
            "functional-disconnection": "target",
            "tract-disconnection": "target",
            "cognet-profile": "target",
            "report": "target",
        },
    ),
    ("ct-normalisation", CT): (
        {
            "lesion-intake": PLANNED,
            "normalisation": PLANNED,
            "functional-disconnection": NOT_REQUESTED,
            "tract-disconnection": NOT_REQUESTED,
            "cognet-profile": NOT_REQUESTED,
            "report": NOT_REQUESTED,
        },
        {"lesion-intake": "target", "normalisation": "target"},
    ),
    ("lesion-qc-report", CT): (
        {
            "lesion-intake": PLANNED,
            "normalisation": PLANNED,
            "functional-disconnection": NOT_REQUESTED,
            "tract-disconnection": NOT_REQUESTED,
            "cognet-profile": NOT_REQUESTED,
            "report": PLANNED,
        },
        {"lesion-intake": "target", "normalisation": "upstream", "report": "target"},
    ),
    ("lesion-qc-report", MNI): (
        {
            "lesion-intake": PLANNED,
            "normalisation": CONDITION_FALSE,
            "functional-disconnection": NOT_REQUESTED,
            "tract-disconnection": NOT_REQUESTED,
            "cognet-profile": NOT_REQUESTED,
            "report": PLANNED,
        },
        {"lesion-intake": "target", "report": "target"},
    ),
}


# Every preset is enabled in the ci profile; the deployment profile offers only the minimum one.
@pytest.mark.parametrize(
    ("profile", "preset_id", "input_set"),
    [("ci", *key) for key in sorted(EXPECTED)] + [("deployment", "mni-mask-analysis", MNI)],
)
def test_plan_statuses_match_the_design_table(documents, pipeline, presets, profile, preset_id, input_set):
    expected_statuses, expected_roles = EXPECTED[(preset_id, input_set)]
    result = plan(
        pipeline,
        input_set,
        PlanRequest(preset=presets.get("stroke-lnm", VERSION, preset_id)),
        enablement(documents[1], profile),
    )
    assert statuses(result) == expected_statuses
    assert {s.slot: s.plan_role for s in result.slots if s.plan_role} == expected_roles
    assert result.preset.id == preset_id


def test_lesion_qc_report_leaves_the_analyses_out(documents, pipeline, presets):
    """The plan of `lesion-qc-report` is the intake and the report (plus normalisation in CT
    mode); tract and functional disconnection are not pulled in, since the report's tract
    port is optional from 0.2.0."""
    en = enablement(documents[1], "ci")
    request = PlanRequest(preset=presets.get("stroke-lnm", VERSION, "lesion-qc-report"))
    for input_set in (CT, MNI):
        result = plan(pipeline, input_set, request, en)
        assert {s.slot for s in result.selected} == (
            {"lesion-intake", "normalisation", "report"}
            if input_set == CT
            else {"lesion-intake", "report"}
        )


def test_mni_mask_analysis_requires_functional_disconnection(documents, pipeline, presets):
    """Unlike `full`, the run is refused (not silently reduced) when functional
    disconnection has no enabled implementation."""
    en = enablement(documents[1], "ci", ("functional-disconnection",))
    request = PlanRequest(preset=presets.get("stroke-lnm", VERSION, "mni-mask-analysis"))
    refused = plan(pipeline, MNI, request, en)
    assert isinstance(refused, PlanRefused)
    assert [(r.code, r.slot) for r in refused.reasons] == [("no_implementation", "functional-disconnection")]
    full_request = PlanRequest(preset=presets.get("stroke-lnm", VERSION, "full"))
    assert statuses(plan(pipeline, MNI, full_request, en))["functional-disconnection"] == (
        "unavailable",
        "no_implementation",
    )


def test_ct_normalisation_is_unavailable_while_normalisation_is_unmounted(documents, pipeline, presets):
    request = PlanRequest(preset=presets.get("stroke-lnm", VERSION, "ct-normalisation"))
    refused = plan(pipeline, CT, request, enablement(documents[1], "ci", ("normalisation",)))
    assert isinstance(refused, PlanRefused)
    assert [(r.code, r.slot) for r in refused.reasons] == [("no_implementation", "normalisation")]
    # and available again once it is mounted
    assert isinstance(plan(pipeline, CT, request, enablement(documents[1], "ci")), Plan)


@pytest.mark.parametrize("preset_id", ["mni-mask-analysis", "ct-normalisation"])
def test_single_input_set_presets_refuse_the_other_one(documents, pipeline, presets, preset_id):
    other = CT if preset_id == "mni-mask-analysis" else MNI
    refused = plan(
        pipeline,
        other,
        PlanRequest(preset=presets.get("stroke-lnm", VERSION, preset_id)),
        enablement(documents[1], "ci"),
    )
    assert isinstance(refused, PlanRefused)
    assert refused.code == "preset_input_set_mismatch"


def test_a_preset_that_is_not_enabled_is_refused(documents, pipeline, presets):
    registry = documents[1]
    en = enablement(registry, "ci")
    off = Enablement(en.enabled, en.defaults, frozenset(PRESETS) - {"full"})
    refused = plan(pipeline, MNI, PlanRequest(preset=presets.get("stroke-lnm", VERSION, "full")), off)
    assert isinstance(refused, PlanRefused) and refused.code == "preset_not_enabled"


# --------------------------------------------------------- the report step's contract

REPORT_MANIFEST = yaml.safe_load((REPO / "steps" / "report" / "step.yaml").read_text(encoding="utf-8"))


def _too_old(pipeline, presets, documents, preset_id, input_set):
    result = plan(
        pipeline,
        input_set,
        PlanRequest(preset=presets.get("stroke-lnm", VERSION, preset_id)),
        enablement(documents[1], "ci"),
    )
    assert isinstance(result, Plan)
    return provenance_consumer_reasons(pipeline, result, {"report": REPORT_MANIFEST})


@pytest.mark.parametrize(
    ("preset_id", "input_set"),
    [("full", CT), ("full", MNI), ("mni-mask-analysis", MNI), ("lesion-qc-report", CT), ("lesion-qc-report", MNI)],
)
def test_presets_with_the_report_are_accepted_by_the_report_step(
    documents, pipeline, presets, preset_id, input_set
):
    """research-report 0.3.0 and later declare contract 1.2 and read `run.provenance`: no preset is
    refused with `step_contract_too_old` any more (card F1-25 lifted the refusal)."""
    assert str(REPORT_MANIFEST["contract_version"]) == "1.2"
    assert _too_old(pipeline, presets, documents, preset_id, input_set) == []


def test_the_pipeline_pins_the_report_step_that_declares_contract_1_2(pipeline):
    report = pipeline.slot("report")
    for impl in report["implementations"]["allowed"]:
        assert impl["step"] == {
            "name": REPORT_MANIFEST["name"],
            "version": REPORT_MANIFEST["version"],
        }


def test_ct_normalisation_reads_no_run_record(documents, pipeline, presets):
    """No planned step reads `run.provenance`: the preset runs today. (The report is not in
    its plan; the other steps never consume the run record.)"""
    assert _too_old(pipeline, presets, documents, "ct-normalisation", CT) == []


def test_only_the_report_reads_the_run_record(pipeline):
    readers = [
        s["name"]
        for s in pipeline.slots
        for c in s["consumes"]
        if "run.provenance" in ([c["from"]] if isinstance(c["from"], str) else c["from"])
    ]
    assert readers == ["report"]


# ------------------------------------------------------------------------ materialize


def test_materialize_copies_the_preset_files_byte_for_byte(tmp_path):
    pipeline, registry = registry_tool.load(HERE)
    digests = {}
    for impls in registry["implementations"].values():
        for entry in impls.values():
            digests[f"{entry['step']['name']}@{entry['step']['version']}"] = (
                f"registry.example/{entry['step']['name']}@sha256:" + "a" * 64
            )
    digests_file = tmp_path / "digests.json"
    digests_file.write_text(json.dumps(digests), encoding="utf-8")
    out = tmp_path / "out"
    assert registry_tool.main(["materialize", "--digests", str(digests_file), "--out", str(out)]) == 0
    for name in PRESETS:
        assert (out / "stroke-lnm" / "presets" / f"{name}.yaml").read_bytes() == (
            HERE / "presets" / f"{name}.yaml"
        ).read_bytes()
    # and the materialized tree loads: presets are attached to the pipeline with its pinned images
    pipelines = DirectoryPipelineRegistry(out)
    assert len(DirectoryPresetRegistry(out, pipelines, StaticStepCatalog()).presets("stroke-lnm", VERSION)) == 4
    shutil.rmtree(out)
