"""Tests for the step-contract schemas, examples and validator (card F0-10)."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from contracts import validate as v

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
EXAMPLES = CONTRACTS / "examples"
VALID = sorted(p for p in EXAMPLES.iterdir() if p.is_file())
INVALID = sorted((EXAMPLES / "invalid").iterdir())

# Expected failure layer for every invalid example: "schema" means the JSON
# Schema alone rejects it; "semantic" means only validate.py's rules do.
INVALID_LAYER = {
    "bundle-no-licence.yaml": "schema",
    "error-missing-message.json": "schema",
    "pipeline-image-by-tag.yaml": "schema",
    "pipeline-type-mismatch.yaml": "semantic",
    "provenance-bad-hash.json": "schema",
    "step-newer-contract.yaml": "semantic",
    "step-parameter-without-default.yaml": "schema",
    "step-tagged-image-bad-version.yaml": "schema",
}


def _errors(issues):
    return [i for i in issues if i.severity == "error"]


def _load(name):
    return v.load_file(EXAMPLES / name)


def _types():
    return yaml.safe_load(
        (CONTRACTS / "artefact-types.yaml").read_text(encoding="utf-8")
    )["types"]


def test_schemas_are_valid_draft_2020_12():
    for name in v.SCHEMA_FILES.values():
        schema = json.loads((v.SCHEMA_DIR / name).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        v.Draft202012Validator.check_schema(schema)


def test_every_invalid_example_has_an_expected_layer():
    assert sorted(p.name for p in INVALID) == sorted(INVALID_LAYER)
    assert len(INVALID) >= 3


@pytest.mark.parametrize(
    "path", VALID + [CONTRACTS / "artefact-types.yaml"], ids=lambda p: p.name
)
def test_valid_examples_pass_alone(path):
    assert _errors(v.validate_files([path])) == []


def test_valid_examples_pass_together_with_cross_checks():
    issues = v.validate_files(VALID)
    assert _errors(issues) == []
    # The two worked step manifests are actually cross-checked against the pipeline.
    notes = " ".join(i.message for i in issues if i.severity == "note")
    assert "ct-lesion-to-mni152" not in notes
    assert "fdisconnection-stub" not in notes


def test_strict_mode_requires_every_manifest():
    issues = v.validate_files(VALID, strict=True)
    assert any("lesion-qc 1.0.0 not supplied" in i.message for i in _errors(issues))


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.name)
def test_invalid_examples_fail_at_the_expected_layer(path):
    errors = _errors(v.validate_files([path]))
    assert errors, f"{path.name} was accepted"
    layers = {e.layer for e in errors}
    assert layers == {INVALID_LAYER[path.name]}, [str(e) for e in errors]


@pytest.mark.parametrize(
    "name", [n for n, layer in INVALID_LAYER.items() if layer == "schema"]
)
def test_schema_alone_rejects_schema_invalid_examples(name):
    data = v.load_file(EXAMPLES / "invalid" / name)
    schema = v.detect_schema(data, Path(name))
    assert v.schema_errors(data, schema)


def test_cli_exit_codes(capsys):
    assert v.main([str(p) for p in VALID]) == 0
    assert v.main([str(EXAMPLES / "invalid" / "bundle-no-licence.yaml")]) == 1
    out = capsys.readouterr().out
    assert "OK" in out and "ERROR" in out


def test_kind_detection_by_filename(tmp_path):
    data = _load("error.json")
    del data["kind"]
    target = tmp_path / "error.json"
    target.write_text(json.dumps(data), encoding="utf-8")
    assert v.detect_schema(data, target) == "error"
    # kind is required, so the file is still rejected - but by the right schema.
    assert any("kind" in i.message for i in v.validate_files([target]))


# ----------------------------------------------------------------- semantic rules


def test_bundle_digest_is_checked():
    data = _load("bundle-toy-normative-connectome.yaml")
    assert v.check_bundle(data) == []
    data["files"][0]["sha256"] = "0" * 64
    assert any("content_digest" in e for e in v.check_bundle(data))


def test_bundle_digest_formula_is_order_independent():
    files = _load("bundle-toy-normative-connectome.yaml")["files"]
    assert v.bundle_digest(files) == v.bundle_digest(list(reversed(files)))


def test_step_parameter_defaults_must_satisfy_schema():
    data = _load("step-ct-lesion-to-mni152.yaml")
    data["parameters"]["properties"]["mask_threshold"]["default"] = 2
    assert any("defaults" in e for e in v.check_step(data, _types()))


def test_step_seed_must_be_integer():
    data = _load("step-fdisconnection-stub.yaml")
    data["parameters"]["properties"]["seed"] = {"type": "number", "default": 0.5}
    assert any("seed" in e for e in v.check_step(data, _types()))


def test_step_unknown_artefact_type():
    data = _load("step-ct-lesion-to-mni152.yaml")
    data["outputs"][0]["type"] = "lesion_mask_mni_1mm"
    assert any("unknown artefact type" in e for e in v.check_step(data, _types()))


def test_step_reference_test_must_cover_required_inputs():
    data = _load("step-ct-lesion-to-mni152.yaml")
    del data["reference_test"]["inputs"]["ct"]
    assert any("required input 'ct'" in e for e in v.check_step(data, _types()))


def test_pipeline_example_is_semantically_valid():
    assert v.check_pipeline(_load("pipeline-stroke-lnm.yaml"), _types()) == []


def test_pipeline_cycle_is_rejected():
    data = _load("pipeline-stroke-lnm.yaml")
    fd = next(s for s in data["slots"] if s["name"] == "functional-disconnection")
    fd["consumes"].append(
        {
            "port": "cognet_profile",
            "type": "cognet_profile",
            "from": "cognitive-profile.cognet_profile",
            "optional": True,
        }
    )
    assert any("cycle" in e for e in v.check_pipeline(data, _types()))


def test_required_slot_cannot_depend_on_optional_slot():
    data = _load("pipeline-stroke-lnm.yaml")
    cp = next(s for s in data["slots"] if s["name"] == "cognitive-profile")
    cp["optional"] = False
    errors = v.check_pipeline(data, _types())
    assert any(
        "required slot 'cognitive-profile'" in e and "may be absent" in e
        for e in errors
    )


def test_mask_must_be_available_in_every_input_set():
    data = _load("pipeline-stroke-lnm.yaml")
    tract = next(s for s in data["slots"] if s["name"] == "tract-disconnection")
    tract["consumes"][0]["from"] = (
        "normalisation.lesion_mask_mni"  # no MNI-mask path any more
    )
    errors = v.check_pipeline(data, _types())
    assert any(
        "input set 'mni-mask'" in e and "tract-disconnection" in e for e in errors
    )


def test_conditional_output_is_required_for_static_availability():
    data = _load("pipeline-stroke-lnm.yaml")
    intake = next(s for s in data["slots"] if s["name"] == "lesion-intake")
    port = next(p for p in intake["produces"] if p["port"] == "lesion_mask_mni")
    del port["when"]
    port["optional"] = True
    errors = v.check_pipeline(data, _types())
    assert any("input set 'mni-mask'" in e and "may be absent" in e for e in errors)


def test_slot_that_never_runs_is_rejected():
    data = _load("pipeline-stroke-lnm.yaml")
    norm = next(s for s in data["slots"] if s["name"] == "normalisation")
    norm["when"] = {"inputs_present": ["ct"], "inputs_absent": ["ct"]}
    assert any("can never run" in e for e in v.check_pipeline(data, _types()))


def test_default_implementation_must_be_allowed():
    data = _load("pipeline-stroke-lnm.yaml")
    data["slots"][0]["implementations"]["default"] = "not-listed"
    assert any("default implementation" in e for e in v.check_pipeline(data, _types()))


def test_same_step_version_cannot_have_two_images():
    data = _load("pipeline-stroke-lnm.yaml")
    norm = next(s for s in data["slots"] if s["name"] == "normalisation")
    norm["implementations"]["allowed"][1]["image"] = (
        norm["implementations"]["allowed"][1]["image"][:-1] + "0"
    )
    assert any("two different images" in e for e in v.check_pipeline(data, _types()))


# ----------------------------------------------------------------- cross-document rules


def _docs(**replace):
    docs = []
    for path in VALID:
        data = copy.deepcopy(replace.get(path.name, v.load_file(path)))
        docs.append(v.Document(path, data, v.detect_schema(data, path)))
    return docs


def test_slot_matching_rule_m1_provides():
    stub = _load("step-fdisconnection-stub.yaml")
    stub["provides"] = "tract-disconnection"
    issues = v.cross_check(
        _docs(**{"step-fdisconnection-stub.yaml": stub}), strict=False
    )
    assert any("(M1)" in i.message for i in _errors(issues))


def test_slot_matching_rule_m2_output_type():
    step = _load("step-ct-lesion-to-mni152.yaml")
    step["outputs"][0]["type"] = "lesion_mask_mni_any"
    issues = v.cross_check(
        _docs(**{"step-ct-lesion-to-mni152.yaml": step}), strict=False
    )
    assert any("(M2)" in i.message for i in _errors(issues))


def test_slot_matching_rule_m3_required_input():
    step = _load("step-ct-lesion-to-mni152.yaml")
    step["inputs"].append({"name": "brain_mask", "type": "lesion_mask_native"})
    issues = v.cross_check(
        _docs(**{"step-ct-lesion-to-mni152.yaml": step}), strict=False
    )
    assert any("(M3)" in i.message for i in _errors(issues))


def test_parameter_binding_must_fit_step_schema():
    step = _load("step-ct-lesion-to-mni152.yaml")
    step["parameters"]["properties"]["age_band"]["type"] = ["integer", "null"]
    step["parameters"]["properties"]["age_band"].pop("pattern")
    issues = v.cross_check(
        _docs(**{"step-ct-lesion-to-mni152.yaml": step}), strict=False
    )
    assert any("case.age_band" in i.message for i in _errors(issues))


def test_bundle_pin_must_satisfy_version_range():
    pipeline = _load("pipeline-stroke-lnm.yaml")
    fd = next(s for s in pipeline["slots"] if s["name"] == "functional-disconnection")
    fd["implementations"]["allowed"][0]["bundles"]["connectome"]["version"] = "2.0.0"
    issues = v.cross_check(
        _docs(**{"pipeline-stroke-lnm.yaml": pipeline}), strict=False
    )
    assert any("not in" in i.message for i in _errors(issues))


def test_bundle_pin_must_have_required_type():
    bundle = _load("bundle-toy-normative-connectome.yaml")
    bundle["type"] = "tract-atlas"
    issues = v.cross_check(
        _docs(**{"bundle-toy-normative-connectome.yaml": bundle}), strict=False
    )
    assert any(
        "step requires 'normative-connectome'" in i.message for i in _errors(issues)
    )


def test_step_provenance_must_match_manifest():
    prov = _load("provenance-step.json")
    prov["outputs"] = prov["outputs"][1:]  # drop the required MNI mask
    issues = v.cross_check(_docs(**{"provenance-step.json": prov}), strict=False)
    assert any(
        "required output 'lesion_mask_mni'" in i.message for i in _errors(issues)
    )


def test_run_provenance_must_match_pipeline():
    run = _load("provenance-run.json")
    run["slots"][0]["implementation"]["image"] = run["slots"][3]["implementation"][
        "image"
    ]
    issues = v.cross_check(_docs(**{"provenance-run.json": run}), strict=False)
    assert any("differs from the pipeline" in i.message for i in _errors(issues))


def test_run_provenance_rejected_step_needs_error():
    run = _load("provenance-run.json")
    run["steps"][0]["execution"]["outcome"] = "rejected"
    assert any("requires an error record" in e for e in v.check_provenance(run))


# ----------------------------------------------------------------- versions


@pytest.mark.parametrize(
    ("version", "spec", "ok"),
    [
        ("1.2.3", ">=1.0.0, <2.0.0", True),
        ("2.0.0", ">=1.0.0, <2.0.0", False),
        ("1.4.0", "^1.2.0", True),
        ("2.0.0", "^1.2.0", False),
        ("1.0.0-rc.1", ">=1.0.0", False),
        ("1.0.0", "==1.0.0", True),
    ],
)
def test_version_ranges(version, spec, ok):
    assert v.satisfies(version, spec) is ok


def test_contract_version_window():
    doc = v.Document(Path("x"), {"contract_version": "1.0"}, "step")
    assert v.check_contract_version(doc) == []
    doc.data["contract_version"] = f"1.{v.CONTRACT_MINOR + 1}"
    assert v.check_contract_version(doc)


@pytest.mark.parametrize(
    ("message", "ok"),
    [
        ("The lesion mask is empty and/or not binary.", True),
        ("Cannot read /in/ct.nii.gz.", False),
        ("Cannot read C:\data\ct.nii.gz.", False),
    ],
)
def test_error_message_must_not_contain_paths(message, ok):
    data = _load("error.json")
    data["message"] = message
    assert (v.check_error(data) == []) is ok
