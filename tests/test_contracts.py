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
    "error-reserved-code-wrong-category.json": "semantic",
    "pipeline-image-by-tag.yaml": "schema",
    "pipeline-type-mismatch.yaml": "semantic",
    "provenance-bad-hash.json": "schema",
    "provenance-rejected-with-outputs.json": "semantic",
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
    doc.data["contract_version"] = "1.1"
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


# ----------------------------------------------------------------- contract 1.1 (ADR-0017)


def _cross_errors(*named):
    """Cross-check documents given as (file name, data) pairs."""
    docs = [
        v.Document(Path(name), data, v.detect_schema(data, Path(name)))
        for name, data in named
    ]
    return [i.message for i in _errors(v.cross_check(docs, strict=False))]


def _step(version="1.1"):
    data = _load("step-ct-lesion-to-mni152.yaml")
    data["contract_version"] = version
    return data


def _error(code, category="input_rejected", version="1.1", port=None):
    data = _load("error.json")
    data.update(contract_version=version, code=code, category=category)
    data.pop("details")
    data.pop("port")
    if port:
        data["port"] = port
    return data


def _rejected_provenance(version="1.1"):
    data = _load("provenance-step-rejected.json")
    data["contract_version"] = version
    return data


def test_contract_version_is_1_1_everywhere():
    assert v.CONTRACT_VERSION == "1.1"
    for path in VALID:
        assert v.load_file(path)["contract_version"] == "1.1", path.name
    sdk = (ROOT / "steps/sdk/src/stroke_step_sdk/contracts.py").read_text("utf-8")
    assert 'SUPPORTED_CONTRACT_VERSION = "1.1"' in sdk


def test_contract_1_0_documents_are_still_accepted(tmp_path):
    paths = []
    for path in VALID:
        if path.name == "error-invalid-parameters.json":
            continue  # an undeclared reserved code is valid only from 1.1 (gap 1)
        data = v.load_file(path)
        data["contract_version"] = "1.0"
        if path.name == "provenance-run.json":
            for step in data["steps"]:
                for key in ("provenance", "error"):
                    if step[key]:
                        step[key]["contract_version"] = "1.0"
        if path.name == "step-ct-lesion-to-mni152.yaml":
            del data["reference_test"]["generator"]  # a 1.1 field (gap 5)
        target = tmp_path / (path.stem + ".json")
        target.write_text(json.dumps(data), encoding="utf-8")
        paths.append(target)
    assert [str(i) for i in _errors(v.validate_files(paths))] == []


# Gap 1: reserved error codes.


@pytest.mark.parametrize(
    "code", ["input_missing", "input_format_invalid", "invalid_parameters"]
)
def test_reserved_rejection_codes_are_implicitly_declared(code):
    step = _step()
    assert code not in {e["code"] for e in step["errors"]}
    assert _cross_errors(("step.yaml", step), ("error.json", _error(code))) == []


def test_reserved_codes_must_be_declared_by_contract_1_0_steps():
    errors = _cross_errors(
        ("step.yaml", _step("1.0")),
        ("error.json", _error("input_missing", version="1.0")),
    )
    assert any("not declared" in e for e in errors)


def test_undeclared_step_code_is_rejected():
    errors = _cross_errors(("step.yaml", _step()), ("error.json", _error("made_up")))
    assert any("'made_up' is not declared" in e for e in errors)


def test_declared_code_must_keep_its_category():
    errors = _cross_errors(
        ("step.yaml", _step()), ("error.json", _error("mask_empty", "qc_failed"))
    )
    assert any("declared with category 'input_rejected'" in e for e in errors)


def test_error_port_must_be_an_input_port():
    errors = _cross_errors(
        ("step.yaml", _step()),
        ("error.json", _error("input_missing", port="lesion_mask_mni")),
    )
    assert any("not an input port" in e for e in errors)


def test_internal_codes_need_no_declaration():
    error = _error("terminated", "internal")
    assert v.check_error(error) == []
    assert _cross_errors(("step.yaml", _step()), ("error.json", error)) == []


@pytest.mark.parametrize(
    ("code", "category"),
    [("input_missing", "qc_failed"), ("terminated", "input_rejected")],
)
def test_reserved_code_category_is_fixed_from_1_1(code, category):
    assert v.check_error(_error(code, category))
    assert v.check_error(_error(code, category, version="1.0")) == []


def test_manifest_may_list_reserved_codes_only_with_their_category():
    step = _step()
    step["errors"].append(
        {"code": "input_missing", "category": "input_rejected", "description": "x"}
    )
    assert v.check_step(step, _types()) == []
    step["errors"][-1]["category"] = "qc_failed"
    assert any("reserved" in e for e in v.check_step(step, _types()))
    del step["reference_test"]["generator"]
    step["contract_version"] = "1.0"
    assert v.check_step(step, _types()) == []  # 1.0 had no reserved codes


# Gap 2: parameters of rejected and failed records.


@pytest.mark.parametrize("status", ["rejected", "failed"])
def test_raw_parameters_are_accepted_unless_succeeded(status):
    prov = _rejected_provenance()
    prov["status"] = status
    assert prov["parameters"]["mask_threshold"] == 1.5  # violates the schema
    assert _cross_errors(("step.yaml", _step()), ("provenance.json", prov)) == []


def test_parameters_of_succeeded_records_are_checked():
    prov = _load("provenance-step.json")
    prov["parameters"]["mask_threshold"] = 1.5
    errors = _cross_errors(("step.yaml", _step()), ("provenance.json", prov))
    assert any(e.startswith("parameters:") for e in errors)


# Gap 3: outputs on rejection and failure.


@pytest.mark.parametrize("status", ["rejected", "failed"])
def test_outputs_must_be_empty_unless_succeeded(status):
    prov = _rejected_provenance()
    prov["status"] = status
    prov["outputs"] = _load("provenance-step.json")["outputs"][:1]
    assert any("outputs must be empty" in e for e in v.check_provenance(prov))
    prov["contract_version"] = "1.0"
    assert v.check_provenance(prov) == []  # 1.0 records keep the 1.0 rules


# Gap 4: SIGTERM (exit code 143, reserved internal code "terminated").


def test_terminated_report_validates_without_declaration():
    error = _error("terminated", "internal")
    error["message"] = "The step was terminated before it finished."
    prov = _rejected_provenance()
    prov["status"] = "failed"
    prov["parameters"] = _load("provenance-step.json")["parameters"]
    assert v.RESERVED_ERROR_CODES["terminated"] == "internal"
    assert (
        _cross_errors(
            ("step.yaml", _step()), ("error.json", error), ("provenance.json", prov)
        )
        == []
    )
    text = (ROOT / "docs/contracts/step-contract.md").read_text(encoding="utf-8")
    assert "`143`" in text and "`terminated`" in text and "30 seconds" in text


# Gap 5: generated reference fixtures.


@pytest.mark.parametrize(
    ("generator", "ok"),
    [
        ("tests/make_reference.py", True),
        ("/abs/make_reference.py", False),
        ("../outside/make_reference.py", False),
        ("tests/make_reference.sh", False),
    ],
)
def test_reference_test_generator_path(generator, ok):
    step = _step()
    step["reference_test"]["generator"] = generator
    assert (v.schema_errors(step, "step") == []) is ok


def test_reference_test_generator_requires_contract_1_1():
    step = _step("1.0")
    assert any("generator" in e for e in v.check_step(step, _types()))
    del step["reference_test"]["generator"]
    assert v.check_step(step, _types()) == []


# Gap 6: software[] is free-form.


@pytest.mark.parametrize(
    "software",
    [[], [{"name": "Some Tool (custom build)", "version": "2024-03 nightly"}]],
)
def test_software_entries_are_free_form(software):
    prov = _load("provenance-step.json")
    prov["software"] = software
    assert v.schema_errors(prov, "provenance") == []
    assert _cross_errors(("step.yaml", _step()), ("provenance.json", prov)) == []
