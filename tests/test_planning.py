"""Run plans in the orchestrator (card F1-19): ``orchestrator.planning`` (pure), the
``PresetRegistry`` of ``orchestrator.catalog`` and the staged run record of
``orchestrator.provenance``. Database and engine tests are in
``tests/test_run_plans.py``.

All pipelines and presets are the repository's own documents or synthetic ones; no
data of any person is involved.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import random
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from contracts import validate as v
from orchestrator.catalog import (
    DirectoryPresetRegistry,
    PresetDefinition,
    PresetError,
    StaticPipelineRegistry,
    StaticPresetRegistry,
    StaticStepCatalog,
    load_preset,
    resolve_presets_dir,
)
from orchestrator.pipeline import PipelineDefinition, condition_holds
from orchestrator.planning import (
    Enablement,
    Plan,
    PlanRefused,
    PlanRequest,
    PresetRef,
    TargetRequest,
    choose_implementation,
    plan,
    provenance_consumer_reasons,
    reason_dict,
    resolve_request,
    targets_errors,
)
from orchestrator.provenance import run_provenance_for_step

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "contracts" / "examples" / "pipeline-stroke-lnm.yaml"


def _definition(data: Mapping) -> PipelineDefinition:
    return PipelineDefinition(data=data, sha256=hashlib.sha256(b"x").hexdigest())


def _example(version: str = "0.1.0") -> PipelineDefinition:
    data = v.load_file(EXAMPLE)
    if version != "0.1.0":
        data["version"] = version
    return _definition(data)


def _all_enabled(pipeline: PipelineDefinition, **overrides) -> Enablement:
    enabled = {
        s["name"]: frozenset(a["id"] for a in s["implementations"]["allowed"])
        for s in pipeline.slots
    }
    enabled.update({k: frozenset(val) for k, val in overrides.items()})
    return Enablement(enabled=enabled, presets=frozenset({"analysis", "fixed-report"}))


def _preset(**changes) -> PresetDefinition:
    data = {
        "contract_version": "1.2",
        "kind": "Preset",
        "id": "analysis",
        "version": "1.0.0",
        "title": "Synthetic analysis",
        "pipeline": {"id": "stroke-lnm", "versions": ">=0.1.0, <0.2.0"},
        "input_sets": ["mni-mask"],
        "targets": {"required": ["tract-disconnection"]},
    }
    data.update(changes)
    raw = yaml.safe_dump(data, sort_keys=False).encode()
    return PresetDefinition(data=data, sha256=hashlib.sha256(raw).hexdigest())


def _codes(result) -> list[str]:
    assert isinstance(result, PlanRefused), result
    return [r.code for r in result.reasons]


def _status(result: Plan) -> dict[str, tuple]:
    return {
        s.slot: (s.status, s.reason, s.plan_role, s.implementation_id, s.chosen_by)
        for s in result.slots
    }


# ----------------------------------------------------------- R0: the previous behaviour


def _legacy_choose(slot, settings, requested):
    """``orchestrator.resolution.choose_implementation`` before F1-19, verbatim apart
    from returning the refusal instead of raising it."""
    allowed = [a["id"] for a in slot["implementations"]["allowed"]]
    enabled = [i for i in allowed if i in settings and settings[i].enabled]
    if requested is not None:
        if requested not in allowed or requested not in enabled:
            return ("refuse", "implementation_not_enabled")
        return requested, "user"
    defaults = [i for i in enabled if settings[i].is_default]
    if defaults:
        return defaults[0], "deployment_default"
    pipeline_default = slot["implementations"].get("default")
    if pipeline_default in enabled:
        return pipeline_default, "pipeline_default"
    return None


def _legacy_resolution(pipeline: PipelineDefinition, present, settings, requested):
    """The slot loop of ``create_run`` before F1-19 (pipeline.md 6 items 1-4), without
    materialisation: ``("refused", code, slot)`` or ``{slot: (status, reason,
    implementation, chosen_by)}``."""
    from orchestrator.resolution import _required_ports_available

    selected, out = {}, {}
    for name in pipeline.topological_order:
        slot = pipeline.slot(name)
        optional = bool(slot.get("optional", False))
        if not condition_holds(slot.get("when"), present):
            out[name] = ("skipped", "condition_false", None, None)
            continue
        if not _required_ports_available(slot, present, selected):
            if not optional:
                return ("refused", "slot_unsatisfiable", name)
            out[name] = ("skipped", "upstream_absent", None, None)
            continue
        choice = _legacy_choose(slot, settings.get(name, {}), requested.get(name))
        if choice is not None and choice[0] == "refuse":
            return ("refused", choice[1], name)
        if choice is None:
            if not optional:
                return ("refused", "no_implementation", name)
            out[name] = ("unavailable", "no_implementation", None, None)
            continue
        out[name] = ("selected", None, choice[0], choice[1])
        selected[name] = slot
    return out


def _random_deployment(pipeline: PipelineDefinition, rng: random.Random):
    """Settings rows and requested implementations, at random: each allowed
    implementation enabled or not, at most one deployment default per slot, and for
    some slots a user choice (allowed and enabled, allowed but disabled, or unknown)."""
    rows, requested = [], {}
    for slot in pipeline.slots:
        allowed = [a["id"] for a in slot["implementations"]["allowed"]]
        enabled = [i for i in allowed if rng.random() < 0.7]
        default = rng.choice(enabled) if enabled and rng.random() < 0.4 else None
        for impl in allowed:
            if impl in enabled or rng.random() < 0.3:  # disabled rows exist too
                rows.append(
                    SimpleNamespace(
                        slot=slot["name"],
                        implementation_id=impl,
                        enabled=impl in enabled,
                        is_default=impl == default,
                    )
                )
        draw = rng.random()
        if draw < 0.15:
            requested[slot["name"]] = rng.choice(allowed)
        elif draw < 0.2:
            requested[slot["name"]] = "not-an-implementation"
    return rows, requested


def _pipelines() -> list[PipelineDefinition]:
    """Every pipeline document in the repository, the worked example in both
    versions, and the synthetic multi-source pipelines of the contract tests."""
    from tests.test_contracts import _repository_pipelines

    return [_definition(d) for d in _repository_pipelines()]


@pytest.mark.parametrize("index", range(len(_pipelines())))
def test_r0_equals_the_previous_run_resolution(index):
    """Property: for every pipeline in the repository, every input set and many
    deployments (enabled implementations, defaults, user choices), the full plan has
    the statuses, implementations and ``chosen_by`` of the run resolution before
    F1-19, and refuses exactly when it did, with the same code and slot."""
    pipeline = _pipelines()[index]
    rng = random.Random(1019 + index)
    for input_set in pipeline.data["input_sets"]:
        present = set(input_set["inputs"])
        for _ in range(400):
            rows, requested = _random_deployment(pipeline, rng)
            settings: dict = {}
            for row in rows:
                settings.setdefault(row.slot, {})[row.implementation_id] = row
            legacy = _legacy_resolution(pipeline, present, settings, requested)
            result = plan(
                pipeline,
                input_set["name"],
                PlanRequest(implementations=requested),
                Enablement.from_settings(rows),
            )
            where = (pipeline.id, input_set["name"], requested, rows)
            if isinstance(legacy, tuple):
                assert isinstance(result, PlanRefused), where
                assert (result.code, result.slot) == legacy[1:], (result, where)
                continue
            assert isinstance(result, Plan), (result, where)
            assert result.mode == "full" and result.targets == {}
            got = {
                s.slot: (s.status, s.reason, s.implementation_id, s.chosen_by)
                for s in result.slots
            }
            assert got == legacy, where
            for s in result.slots:  # roles: presets.md 4.1
                applicable = s.reason != "condition_false"
                assert (s.plan_role is not None) == applicable, (s, where)


def test_r0_roles_and_plan_record():
    pipeline = _example()
    result = plan(pipeline, "mni-mask", PlanRequest(), _all_enabled(pipeline))
    assert _status(result) == {
        "lesion-intake": ("selected", None, "target", "lesion-qc", "pipeline_default"),
        "normalisation": ("skipped", "condition_false", None, None, None),
        # No deployment or pipeline default: nothing can be chosen (as before F1-19).
        "functional-disconnection": (
            "unavailable",
            "no_implementation",
            "if_available",
            None,
            None,
        ),
        "tract-disconnection": (
            "selected",
            None,
            "target",
            "lnm-tracts",
            "pipeline_default",
        ),
        "cognitive-profile": ("skipped", "upstream_absent", "if_available", None, None),
        "report": ("selected", None, "target", "research-report", "pipeline_default"),
    }
    assert not result.needs_contract_1_2


# ------------------------------------------------------------------ choice rule


def test_choice_rule_order_with_presets():
    pipeline = _example()
    slot = pipeline.slot("normalisation")
    both = Enablement(
        enabled={
            "normalisation": frozenset(
                {"ct-lesion-to-mni152", "ct-lesion-to-mni152-no-ventricle-removal"}
            )
        },
        defaults={
            "normalisation": frozenset({"ct-lesion-to-mni152-no-ventricle-removal"})
        },
    )
    alt = "ct-lesion-to-mni152-no-ventricle-removal"
    base = "ct-lesion-to-mni152"
    assert choose_implementation(slot, both, None) == (alt, "deployment_default")
    assert choose_implementation(slot, both, base) == (base, "user")
    # A preset default comes after the user and before the deployment default.
    assert choose_implementation(slot, both, None, ("default", base)) == (
        base,
        "preset",
    )
    assert choose_implementation(slot, both, alt, ("default", base)) == (alt, "user")
    # A fixed choice wins; if it is not enabled nothing replaces it.
    assert choose_implementation(slot, both, None, ("fixed", base)) == (base, "preset")
    only_alt = Enablement(enabled={"normalisation": frozenset({alt})})
    assert choose_implementation(slot, only_alt, None, ("fixed", base)) is None
    # A preset default that is not enabled falls through to the next rule.
    assert choose_implementation(slot, only_alt, None, ("default", base)) is None
    refused = choose_implementation(slot, only_alt, base)
    assert (
        refused.code == "implementation_not_enabled" and refused.slot == "normalisation"
    )


# ------------------------------------------------------------- presets and free mode


def test_preset_plan_marks_slots_outside_the_closure_not_requested():
    pipeline = _example()
    preset = _preset(
        targets={
            "required": ["tract-disconnection"],
            "if_available": ["cognitive-profile"],
        },
        implementations={"tract-disconnection": {"default": "lnm-tracts"}},
    )
    result = plan(
        pipeline, "mni-mask", PlanRequest(preset=preset), _all_enabled(pipeline)
    )
    assert (
        isinstance(result, Plan) and result.mode == "preset" and result.preset is preset
    )
    assert _status(result) == {
        "lesion-intake": (
            "selected",
            None,
            "upstream",
            "lesion-qc",
            "pipeline_default",
        ),
        "normalisation": ("skipped", "condition_false", None, None, None),
        "functional-disconnection": (
            "unavailable",
            "no_implementation",
            "if_available",
            None,
            None,
        ),
        "tract-disconnection": ("selected", None, "target", "lnm-tracts", "preset"),
        "cognitive-profile": ("skipped", "upstream_absent", "if_available", None, None),
        "report": ("skipped", "not_requested", None, None, None),
    }
    assert result.targets == {
        "required": ["tract-disconnection.tract_disconnection"],
        "if_available": ["cognitive-profile.cognet_profile"],
    }
    assert result.needs_contract_1_2
    assert targets_errors(result.targets) == []


def test_free_plan_adds_upstream_slots():
    pipeline = _example()
    request = PlanRequest(targets=TargetRequest(("tract_disconnection",)))
    result = plan(pipeline, "ct-and-native-mask", request, _all_enabled(pipeline))
    assert result.mode == "free"
    assert {s.slot: s.plan_role for s in result.selected} == {
        "normalisation": "upstream",
        "tract-disconnection": "target",
    }
    assert result.slot("lesion-intake").reason == "not_requested"
    assert result.slot("report").reason == "not_requested"
    assert result.targets == {"required": ["tract-disconnection.tract_disconnection"]}


# One test per refusal code (run-plans.md 3.3, presets.md 4.2).


def test_refusal_preset_and_targets():
    refused = resolve_request(
        _example(), preset=PresetRef("analysis"), targets=TargetRequest(("report",))
    )
    assert _codes(refused) == ["preset_and_targets"]


def test_refusal_preset_not_found():
    pipeline = _example()
    registry = StaticPresetRegistry([_preset()], StaticPipelineRegistry([pipeline]))
    assert resolve_request(
        pipeline, preset=PresetRef("analysis"), presets=registry
    ).preset
    for ref, presets in (
        (PresetRef("other"), registry),
        (PresetRef("analysis", "9.9.9"), registry),
        (PresetRef("analysis"), None),
    ):
        refused = resolve_request(pipeline, preset=ref, presets=presets)
        assert _codes(refused) == ["preset_not_found"]
        assert refused.reasons[0].target == ref.id


def test_refusal_preset_not_enabled():
    pipeline = _example()
    enablement = Enablement(enabled=_all_enabled(pipeline).enabled)  # no preset enabled
    refused = plan(pipeline, "mni-mask", PlanRequest(preset=_preset()), enablement)
    assert _codes(refused) == ["preset_not_enabled"]


def test_refusal_preset_input_set_mismatch_with_every_reason():
    pipeline = _example()
    enablement = Enablement(enabled=_all_enabled(pipeline).enabled)
    refused = plan(
        pipeline, "ct-and-native-mask", PlanRequest(preset=_preset()), enablement
    )
    assert _codes(refused) == ["preset_not_enabled", "preset_input_set_mismatch"]
    assert refused.code == "preset_not_enabled"


def test_refusal_implementation_fixed_by_preset():
    pipeline = _example()
    preset = _preset(
        targets={"required": ["report"]},
        implementations={"report": {"fixed": "research-report"}},
    )
    request = PlanRequest(preset=preset, implementations={"report": "other-report"})
    refused = plan(pipeline, "mni-mask", request, _all_enabled(pipeline))
    assert _codes(refused)[0] == "implementation_fixed_by_preset"
    assert refused.slot == "report"
    # The same choice as the fixed one is accepted and recorded as the preset's.
    request = PlanRequest(preset=preset, implementations={"report": "research-report"})
    ok = plan(pipeline, "mni-mask", request, _all_enabled(pipeline))
    assert ok.slot("report").chosen_by == "preset"


def test_fixed_choice_is_only_checked_for_slots_of_the_plan():
    """A preset that fixes a slot planned only under another input set: a different
    user choice for it is ignored where the slot does not run."""
    pipeline = _example()
    alt = "ct-lesion-to-mni152-no-ventricle-removal"
    preset = _preset(
        input_sets=["mni-mask", "ct-and-native-mask"],
        targets={"required": ["tract-disconnection"]},
        implementations={"normalisation": {"fixed": "ct-lesion-to-mni152"}},
    )
    request = PlanRequest(preset=preset, implementations={"normalisation": alt})
    ok = plan(pipeline, "mni-mask", request, _all_enabled(pipeline))
    assert isinstance(ok, Plan)
    assert ok.slot("normalisation").reason == "condition_false"
    refused = plan(pipeline, "ct-and-native-mask", request, _all_enabled(pipeline))
    assert [(r.code, r.slot) for r in refused.reasons] == [
        ("implementation_fixed_by_preset", "normalisation")
    ]
    # Outside the plan (not_requested) it is ignored too.
    preset = _preset(
        input_sets=["ct-and-native-mask"],
        targets={"required": ["lesion-intake"]},
        implementations={"normalisation": {"fixed": "ct-lesion-to-mni152"}},
    )
    request = PlanRequest(preset=preset, implementations={"normalisation": alt})
    ok = plan(pipeline, "ct-and-native-mask", request, _all_enabled(pipeline))
    assert ok.slot("normalisation").reason == "not_requested"


def test_refusal_implementation_not_enabled():
    pipeline = _example()
    request = PlanRequest(
        targets=TargetRequest(("report",)),
        implementations={"tract-disconnection": "not-an-implementation"},
    )
    refused = plan(pipeline, "mni-mask", request, _all_enabled(pipeline))
    assert [(r.code, r.slot, r.required_by) for r in refused.reasons] == [
        ("implementation_not_enabled", "tract-disconnection", "report")
    ]
    # A choice for a slot outside the plan is not looked at.
    request = PlanRequest(
        targets=TargetRequest(("lesion-intake",)),
        implementations={"tract-disconnection": "not-an-implementation"},
    )
    assert isinstance(plan(pipeline, "mni-mask", request, _all_enabled(pipeline)), Plan)


@pytest.mark.parametrize(
    ("required", "if_available", "input_set", "code"),
    [
        (["no-such-slot"], [], "mni-mask", "unknown_target"),
        (["qc_figure"], [], "ct-and-native-mask", "ambiguous_target"),
        (["normalisation"], [], "mni-mask", "target_not_applicable"),
        (["normalisation.ct_mni"], [], "ct-and-native-mask", "target_not_guaranteed"),
        (["report"], ["report"], "mni-mask", "conflicting_targets"),
        ([], ["normalisation"], "mni-mask", "empty_request"),
    ],
)
def test_refusal_target_resolution(required, if_available, input_set, code):
    pipeline = _example()
    request = PlanRequest(targets=TargetRequest(tuple(required), tuple(if_available)))
    refused = plan(pipeline, input_set, request, _all_enabled(pipeline))
    assert _codes(refused) == [code]
    assert refused.mode == "free"


def test_refusal_unknown_target_for_a_non_string():
    refused = resolve_request(_example(), targets=TargetRequest((3,)))
    assert _codes(refused) == ["unknown_target"]


def test_refusal_target_unreachable():
    from tests.test_contracts import _synthetic

    data = _synthetic(fallback_input=True)
    data["input_sets"] = [{"name": "mni", "inputs": ["lesion_mni"]}]
    z = next(s for s in data["slots"] if s["name"] == "z")
    z["consumes"][0]["from"] = "inputs.mask"
    pipeline = _definition(data)
    enabled = _all_enabled(pipeline).enabled
    enablement = Enablement(enabled=enabled, defaults=enabled)  # no pipeline defaults
    request = PlanRequest(targets=TargetRequest(("z",)))
    refused = plan(pipeline, "mni", request, enablement)
    assert [(r.code, r.slot, r.port) for r in refused.reasons] == [
        ("target_unreachable", "z", "m")
    ]
    # The full plan refuses it as before plans existed.
    refused = plan(pipeline, "mni", PlanRequest(), enablement)
    assert (refused.code, refused.slot) == ("slot_unsatisfiable", "z")


def test_refusal_no_implementation_names_the_target_and_every_reason():
    pipeline = _example()
    enablement = _all_enabled(pipeline, **{"tract-disconnection": (), "report": ()})
    request = PlanRequest(targets=TargetRequest(("report",)))
    refused = plan(pipeline, "mni-mask", request, enablement)
    assert {(r.code, r.slot, r.target) for r in refused.reasons} == {
        ("no_implementation", "tract-disconnection", "report"),
        ("no_implementation", "report", "report"),
    }
    assert all(
        set(reason_dict(r)) >= {"code", "message", "slot"} for r in refused.reasons
    )
    # The full plan: the code and slot of the previous run creation.
    refused = plan(pipeline, "mni-mask", PlanRequest(), enablement)
    assert (refused.code, refused.slot) == ("no_implementation", "tract-disconnection")


OLD_REPORT = {
    "name": "research-report",
    "version": "0.2.0",
    "contract_version": "1.0",
    "inputs": [{"name": "run_provenance", "type": "run_provenance"}],
}


def _gate(pipeline, result, manifest=OLD_REPORT):
    return [
        (r.code, r.slot)
        for r in provenance_consumer_reasons(pipeline, result, {"report": manifest})
    ]


def test_refusal_step_contract_too_old_for_a_targeted_plan():
    """Trigger: plan mode preset or free, even with no slot outside the plan and no
    preset choice (a 1.0 record would read as a full run)."""
    pipeline = _example()
    request = PlanRequest(
        targets=TargetRequest(
            ("report",), ("functional-disconnection", "cognitive-profile")
        )
    )
    enablement = _all_enabled(pipeline)
    enablement = Enablement(enablement.enabled, defaults=enablement.enabled)
    result = plan(pipeline, "mni-mask", request, enablement)
    assert result.mode == "free"
    assert not any(
        s.reason == "not_requested" or s.chosen_by == "preset" for s in result.slots
    )
    assert _gate(pipeline, result) == [("step_contract_too_old", "report")]
    assert _gate(pipeline, result, dict(OLD_REPORT, contract_version="1.2")) == []
    # A step that does not declare the run.provenance input does not read it.
    assert _gate(pipeline, result, dict(OLD_REPORT, inputs=[])) == []


def test_refusal_step_contract_too_old_for_not_requested_slots():
    """Trigger: a slot outside the plan (isolated on a full-mode plan object)."""
    pipeline = _example()
    full = plan(pipeline, "mni-mask", PlanRequest(), _all_enabled(pipeline))
    slots = tuple(
        dataclasses.replace(s, status="skipped", reason="not_requested", plan_role=None)
        if s.slot == "cognitive-profile"
        else s
        for s in full.slots
    )
    assert _gate(pipeline, dataclasses.replace(full, slots=slots)) == [
        ("step_contract_too_old", "report")
    ]


def test_refusal_step_contract_too_old_for_preset_choices():
    """Trigger: an implementation chosen by a preset (isolated on a full-mode plan
    object)."""
    pipeline = _example()
    full = plan(pipeline, "mni-mask", PlanRequest(), _all_enabled(pipeline))
    slots = tuple(
        dataclasses.replace(s, chosen_by="preset") if s.slot == "lesion-intake" else s
        for s in full.slots
    )
    assert _gate(pipeline, dataclasses.replace(full, slots=slots)) == [
        ("step_contract_too_old", "report")
    ]


def test_full_plan_never_triggers_step_contract_too_old():
    """The full plan has a truthful older form: a step declaring 1.0 gets the 1.0
    record (relabelled, without the plan fields)."""
    pipeline = _example()
    full = plan(pipeline, "mni-mask", PlanRequest(), _all_enabled(pipeline))
    assert full.mode == "full" and not full.needs_contract_1_2
    assert _gate(pipeline, full) == []


def test_targets_shape():
    assert targets_errors({}) == []
    assert targets_errors({"required": ["a-b.c_d"], "if_available": []}) == []
    assert targets_errors([]) == ["targets must be an object"]
    assert targets_errors({"required": ["a", "a"], "x": []}) == [
        "unknown key 'x'",
        "'required' has duplicates",
        "'required': 'a' is not a slot.port",
        "'required': 'a' is not a slot.port",
    ]


# ------------------------------------------------------------------ preset registry


PRESET_YAML = """\
contract_version: "1.2"
kind: Preset
id: {id}
version: {version}
title: Synthetic preset
pipeline: {{id: stroke-lnm, versions: "{range}"}}
input_sets: [mni-mask]
targets:
  required: [{target}]
"""


def _write(root: Path, name: str, subdir: str = "stroke-lnm", **fields) -> Path:
    values = {
        "id": "analysis",
        "version": "1.0.0",
        "range": ">=0.1.0",
        "target": "report",
    }
    values.update(fields)
    path = root / subdir / "presets" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(PRESET_YAML.format(**values).encode())
    return path


def test_load_preset_checks_the_document(tmp_path):
    path = _write(tmp_path, "analysis.yaml")
    preset = load_preset(path)
    assert (preset.id, preset.version, preset.pipeline_id) == (
        "analysis",
        "1.0.0",
        "stroke-lnm",
    )
    assert preset.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(PresetError, match="1.2"):
        load_preset(path.read_bytes().replace(b'"1.2"', b'"1.1"'))
    with pytest.raises(PresetError, match="kind"):
        load_preset(b"kind: Pipeline\n")


def test_load_preset_requires_file_stem_equal_to_id(tmp_path):
    wrong = _write(tmp_path, "renamed.yaml")  # id is "analysis"
    with pytest.raises(PresetError, match="does not match its id 'analysis'"):
        load_preset(wrong)
    with pytest.raises(PresetError, match="renamed.yaml"):
        load_preset(str(wrong))
    # Bytes carry no file name: nothing to compare.
    assert load_preset(wrong.read_bytes()).id == "analysis"


def test_directory_registry_ignores_a_preset_whose_stem_differs_from_its_id(
    tmp_path, caplog
):
    _write(tmp_path, "renamed.yaml")
    registry = DirectoryPresetRegistry(
        tmp_path, StaticPipelineRegistry([_example()]), StaticStepCatalog()
    )
    assert registry.get("stroke-lnm", "0.1.0", "analysis") is None
    assert registry.get("stroke-lnm", "0.1.0", "renamed") is None
    assert "Ignoring invalid preset" in caplog.text
    assert "does not match its id" in caplog.text


def test_resolve_presets_dir():
    assert resolve_presets_dir("/p", {}) == Path("/p")
    assert resolve_presets_dir("/p", {"PRESETS_DIR": ""}) == Path("/p")
    assert resolve_presets_dir("/p", {"PRESETS_DIR": "/q"}) == Path("/q")


def test_directory_registry_attaches_presets_to_versions(tmp_path, caplog):
    v1, v2 = _example("0.1.0"), _example("0.2.0")
    pipelines = StaticPipelineRegistry([v1, v2])
    _write(tmp_path, "analysis.yaml")
    _write(tmp_path, "analysis.yaml", "zsecond", version="2.0.0", range=">=0.2.0")
    _write(
        tmp_path, "broken.yaml", id="broken", target="normalisation"
    )  # P4 on mni-mask
    (tmp_path / "stroke-lnm" / "presets" / "junk.yaml").write_text("not: [a preset")
    (tmp_path / "stroke-lnm" / "other.yaml").write_text(
        "ignored: true"
    )  # not in presets/
    registry = DirectoryPresetRegistry(tmp_path, pipelines, StaticStepCatalog())
    assert [(p.id, p.version) for p in registry.presets("stroke-lnm", "0.1.0")] == [
        ("analysis", "1.0.0")
    ]
    assert [(p.id, p.version) for p in registry.presets("stroke-lnm", "0.2.0")] == [
        ("analysis", "1.0.0"),
        ("analysis", "2.0.0"),
    ]
    assert registry.get("stroke-lnm", "0.2.0", "analysis").version == "2.0.0"
    assert registry.get("stroke-lnm", "0.2.0", "analysis", "1.0.0").version == "1.0.0"
    assert registry.get("stroke-lnm", "0.1.0", "analysis", "2.0.0") is None
    assert registry.get("stroke-lnm", "0.1.0", "broken") is None
    assert registry.get("other", "0.1.0", "analysis") is None
    assert "Ignoring invalid preset" in caplog.text
    assert "not offered" in caplog.text


def test_registry_rule_p8(tmp_path, caplog):
    pipelines = StaticPipelineRegistry([_example()])
    first = _write(tmp_path, "analysis.yaml")
    _write(tmp_path, "analysis.yaml", "copy", target="tract-disconnection")
    assert (
        DirectoryPresetRegistry(tmp_path, pipelines).presets("stroke-lnm", "0.1.0")
        == []
    )
    assert "defined in 2 files" in caplog.text
    (tmp_path / "copy" / "presets" / "analysis.yaml").unlink()
    digest = hashlib.sha256(first.read_bytes()).hexdigest()
    key = ("stroke-lnm", "analysis", "1.0.0")
    assert DirectoryPresetRegistry(tmp_path, pipelines, recorded={key: digest}).get(
        "stroke-lnm", "0.1.0", "analysis"
    )
    changed = DirectoryPresetRegistry(tmp_path, pipelines, recorded={key: "0" * 64})
    assert changed.get("stroke-lnm", "0.1.0", "analysis") is None
    assert "needs a new version" in caplog.text


def test_registry_refuses_a_fixed_stub():
    pipeline = _example()
    impl = pipeline.slot("functional-disconnection")["implementations"]["allowed"][0]
    stub = {
        "name": impl["step"]["name"],
        "version": impl["step"]["version"],
        "stub": True,
    }
    preset = _preset(
        targets={"required": ["report"], "if_available": ["functional-disconnection"]},
        implementations={"functional-disconnection": {"fixed": impl["id"]}},
    )
    pipelines = StaticPipelineRegistry([pipeline])
    assert StaticPresetRegistry([preset], pipelines).get(
        "stroke-lnm", "0.1.0", "analysis"
    )
    assert (
        StaticPresetRegistry([preset], pipelines, StaticStepCatalog([stub])).get(
            "stroke-lnm", "0.1.0", "analysis"
        )
        is None
    )


# --------------------------------------------------- run record for older steps


def _record() -> dict:
    record = v.load_file(ROOT / "contracts" / "examples" / "provenance-run-plan.json")
    return copy.deepcopy(record)


def test_run_record_for_a_step_of_the_current_contract_is_unchanged():
    record = _record()
    assert run_provenance_for_step(record, {"contract_version": "1.2"}) is record


def test_run_record_for_an_older_step_leaves_out_the_plan():
    record = _record()
    record["plan"] = {"mode": "full", "preset": None, "targets": {}}
    for slot in record["slots"]:
        if slot.get("reason") == "not_requested":
            slot["reason"] = "upstream_absent"
            slot["plan_role"] = "if_available"
        if slot.get("implementation", {}).get("chosen_by") == "preset":
            slot["implementation"]["chosen_by"] = "pipeline_default"
    assert not v.check_provenance(record) and not v.schema_errors(record, "provenance")
    older = run_provenance_for_step(record, {"contract_version": "1.0"})
    assert older["contract_version"] == "1.0" and "plan" not in older
    assert not any("plan_role" in s for s in older["slots"])
    assert not v.schema_errors(older, "provenance") and not v.check_provenance(older)
    assert "plan" in record  # the input is not changed


def test_run_record_with_values_that_need_1_2_is_not_altered():
    record = _record()
    assert any(s.get("reason") == "not_requested" for s in record["slots"]) or any(
        s.get("implementation", {}).get("chosen_by") == "preset"
        for s in record["slots"]
    )
    assert run_provenance_for_step(record, {"contract_version": "1.1"}) is record


def _full_record() -> dict:
    record = _record()
    record["plan"] = {"mode": "full", "preset": None, "targets": {}}
    for slot in record["slots"]:
        if slot.get("implementation", {}).get("chosen_by") == "preset":
            slot["implementation"]["chosen_by"] = "pipeline_default"
    return record


def test_a_1_1_record_is_relabelled_for_a_step_declaring_1_0():
    """A step always gets the record of the contract it declares, also when the
    record itself is older than 1.2 (review fix)."""
    record = _full_record()
    for item in record["inputs"]:
        item.pop("origin", None)
    record.pop("plan")
    for slot in record["slots"]:
        slot.pop("plan_role", None)
        slot.get("implementation", {}).pop("upstream", None)
    record["contract_version"] = "1.1"
    assert not v.check_provenance(record) and not v.schema_errors(record, "provenance")
    older = run_provenance_for_step(record, {"contract_version": "1.0"})
    assert older["contract_version"] == "1.0"
    assert {k: val for k, val in older.items() if k != "contract_version"} == {
        k: val for k, val in record.items() if k != "contract_version"
    }
    assert run_provenance_for_step(record, {"contract_version": "1.1"}) is record


def test_a_targeted_plan_record_is_not_altered_for_an_older_step():
    record = _full_record()
    record["plan"] = {
        "mode": "free",
        "preset": None,
        "targets": {"required": ["report.report_json"]},
    }
    assert run_provenance_for_step(record, {"contract_version": "1.0"}) is record
