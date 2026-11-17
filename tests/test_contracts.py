"""Tests for the step-contract schemas, examples and validator (cards F0-10, F1-17)."""

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
    "preset-contract-1-1.yaml": "semantic",
    "preset-default-and-fixed.yaml": "schema",
    "provenance-bad-hash.json": "schema",
    "provenance-origin-free-text.json": "schema",
    "provenance-rejected-with-outputs.json": "semantic",
    "provenance-run-plan-contract-1-1.json": "semantic",
    "step-newer-contract.yaml": "semantic",
    "step-parameter-without-default.yaml": "schema",
    "step-tagged-image-bad-version.yaml": "schema",
    "step-upstream-contract-1-1.yaml": "semantic",
}
# Valid examples that use fields or values added in contract 1.2 (ADR-0018).
EXAMPLES_1_2 = {
    "preset-mni-mask-analysis.yaml",
    "provenance-run-plan.json",
    "step-lesion-segmenter-wrapper.yaml",
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
    doc.data["contract_version"] = "1.2"
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


def test_contract_version_is_1_2_and_examples_declare_their_version():
    assert v.CONTRACT_VERSION == "1.2"
    for path in VALID:
        expected = "1.2" if path.name in EXAMPLES_1_2 else "1.1"
        assert v.load_file(path)["contract_version"] == expected, path.name
    sdk = (ROOT / "steps/sdk/src/stroke_step_sdk/contracts.py").read_text("utf-8")
    assert 'SUPPORTED_CONTRACT_VERSION = "1.2"' in sdk


def test_contract_1_0_documents_are_still_accepted(tmp_path):
    paths = []
    for path in VALID:
        if path.name == "error-invalid-parameters.json":
            continue  # an undeclared reserved code is valid only from 1.1 (gap 1)
        if path.name in EXAMPLES_1_2:
            continue  # covered by test_contract_1_1_documents_are_still_accepted
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


# ----------------------------------------------------------------- contract 1.2 (ADR-0018)


def _pipeline(version="0.1.0"):
    """The worked pipeline; 0.2.0 makes the report's tract table optional (card F1-26)."""
    data = _load("pipeline-stroke-lnm.yaml")
    if version != "0.1.0":
        data["version"] = version
        report = next(s for s in data["slots"] if s["name"] == "report")
        port = next(c for c in report["consumes"] if c["port"] == "tract_disconnection")
        port["optional"] = True
        assert v.check_pipeline(data, _types()) == []
    return data


def _preset(**changes):
    data = _load("preset-mni-mask-analysis.yaml")
    data.update(changes)
    return data


def _plan_record():
    return _load("provenance-run-plan.json")


def _slot(record, name):
    return next(s for s in record["slots"] if s["slot"] == name)


def _strip_1_2(data):
    """A copy of a 1.2 example without the 1.2 fields, declaring 1.1."""
    data = copy.deepcopy(data)
    data["contract_version"] = "1.1"
    data.pop("upstream", None)
    data.pop("plan", None)
    for i in data.get("inputs", []):
        if isinstance(i, dict):
            i.pop("origin", None)
    for s in data.get("slots", []):
        s.pop("plan_role", None)
        impl = s.get("implementation")
        if impl:
            impl.pop("upstream", None)
            if impl["chosen_by"] == "preset":
                impl["chosen_by"] = "pipeline_default"
    return data


def test_contract_1_1_documents_are_still_accepted(tmp_path):
    """Every 1.1 example validates unchanged, and the 1.2 examples without their 1.2
    fields are valid 1.1 documents: 1.2 only adds."""
    old = [p for p in VALID if p.name not in EXAMPLES_1_2]
    assert _errors(v.validate_files(old)) == []
    paths = []
    for name in ("provenance-run-plan.json", "step-lesion-segmenter-wrapper.yaml"):
        target = tmp_path / (Path(name).stem + ".json")
        target.write_text(json.dumps(_strip_1_2(_load(name))), encoding="utf-8")
        paths.append(target)
    assert [str(i) for i in _errors(v.validate_files(paths))] == []


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (lambda r: r.pop("plan"), "plan requires"),
        (lambda r: r["inputs"][0].pop("origin"), "origin requires"),
        (lambda r: _slot(r, "report").pop("plan_role"), "plan_role requires"),
    ],
)
def test_1_2_run_fields_require_contract_1_2(change, expected):
    record = _plan_record()
    record["contract_version"] = "1.1"
    assert any(expected in e for e in v.check_provenance(record))
    change(record)  # the other 1.2 fields are still reported
    assert v.check_provenance(record)


@pytest.mark.parametrize(
    "field",
    ["not_requested", "chosen_by_preset", "upstream", "plan_role"],
)
def test_each_1_2_slot_value_requires_contract_1_2(field):
    record = _strip_1_2(_plan_record())
    assert v.check_provenance(record) == []
    if field == "not_requested":
        record["slots"][4] = {
            "slot": "cognitive-profile",
            "status": "skipped",
            "reason": "not_requested",
        }
    elif field == "chosen_by_preset":
        _slot(record, "report")["implementation"]["chosen_by"] = "preset"
    elif field == "upstream":
        _slot(record, "report")["implementation"]["upstream"] = _load(
            "step-lesion-segmenter-wrapper.yaml"
        )["upstream"]
    else:
        _slot(record, "report")["plan_role"] = "target"
    assert v.schema_errors(record, "provenance") == []
    assert any("requires contract_version 1.2" in e for e in v.check_provenance(record))


def test_step_upstream_block():
    step = _load("step-lesion-segmenter-wrapper.yaml")
    assert v.check_step(step, _types()) == []
    step["licence"]["third_party"][0]["spdx"] = "MIT"
    assert any("licence.third_party" in e for e in v.check_step(step, _types()))
    step = _load("step-lesion-segmenter-wrapper.yaml")
    step["contract_version"] = "1.1"
    assert any(
        "requires contract_version 1.2" in e for e in v.check_step(step, _types())
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("image", "docker.io/example/segmenter:2.1.0"),  # pinned by digest only
        ("homepage", "http://example.org/example-segmenter"),
        ("unknown", "x"),
    ],
)
def test_step_upstream_block_schema(key, value):
    step = _load("step-lesion-segmenter-wrapper.yaml")
    step["upstream"][key] = value
    assert v.schema_errors(step, "step")


def test_step_upstream_block_requires_name_version_homepage_image_licence():
    for key in ("name", "version", "homepage", "image", "licence"):
        step = _load("step-lesion-segmenter-wrapper.yaml")
        del step["upstream"][key]
        assert v.schema_errors(step, "step"), key
    step = _load("step-lesion-segmenter-wrapper.yaml")
    del step["upstream"]["citation"]
    assert v.schema_errors(step, "step") == []


# Declared input origins.


@pytest.mark.parametrize(
    ("origin", "ok"),
    [
        ({"kind": "manual"}, True),
        (
            {
                "kind": "external-tool",
                "tool": "example-segmenter",
                "version": "2.1.0+cu12",
            },
            True,
        ),
        ({"kind": "external-tool", "tool": "example-segmenter"}, False),
        ({"kind": "manual", "tool": "pen"}, False),
        (
            {"kind": "external-tool", "tool": "tool name with spaces", "version": "1"},
            False,
        ),
        ({"kind": "external-tool", "tool": "t", "version": "x" * 65}, False),
        # Reusing artefacts of earlier runs is deferred (owner decision 6).
        (
            {"kind": "platform-run", "run_id": "0b7e9d52-6a14-4f3c-8e2d-91c5a7b3f604"},
            False,
        ),
    ],
)
def test_declared_input_origin(origin, ok):
    record = _plan_record()
    record["inputs"][0]["origin"] = origin
    assert (v.schema_errors(record, "provenance") == []) is ok


# Plan records in RunProvenance.


def _plan_errors(record):
    assert v.schema_errors(record, "provenance") == []
    return v.check_provenance(record)


def test_plan_record_example_is_consistent():
    assert _plan_errors(_plan_record()) == []


def test_not_requested_slot():
    record = _plan_record()
    record["slots"][4] = {
        "slot": "cognitive-profile",
        "status": "skipped",
        "reason": "not_requested",
    }
    record["plan"]["targets"].pop("if_available")
    assert _plan_errors(record) == []
    _slot(record, "cognitive-profile")["status"] = "unavailable"
    assert any("requires status 'skipped'" in e for e in _plan_errors(record))
    _slot(record, "cognitive-profile")["status"] = "skipped"
    _slot(record, "cognitive-profile")["plan_role"] = "if_available"
    assert any("has no plan_role" in e for e in _plan_errors(record))


def test_not_requested_needs_a_preset_or_free_plan():
    record = _plan_record()
    record["slots"][4] = {
        "slot": "cognitive-profile",
        "status": "skipped",
        "reason": "not_requested",
    }
    for slot in record["slots"]:
        if slot.get("implementation", {}).get("chosen_by") == "preset":
            slot["implementation"]["chosen_by"] = "pipeline_default"
    record["plan"] = {"mode": "full", "preset": None, "targets": {}}
    assert any("not_requested" in e for e in _plan_errors(record))
    del record["plan"]
    for slot in record["slots"]:
        slot.pop("plan_role", None)
    assert any("not_requested" in e for e in _plan_errors(record))


def test_full_plan_record():
    record = _plan_record()
    for slot in record["slots"]:
        if slot.get("implementation", {}).get("chosen_by") == "preset":
            slot["implementation"]["chosen_by"] = "pipeline_default"
    record["plan"] = {"mode": "full", "preset": None, "targets": {}}
    assert _plan_errors(record) == []
    record["plan"]["targets"] = {"required": ["report.report_pdf"]}
    assert any("full plan has no targets" in e for e in _plan_errors(record))


@pytest.mark.parametrize(
    ("mode", "preset", "message"),
    [
        ("preset", None, "exactly when mode is 'preset'"),
        ("free", {"id": "x", "version": "1.0.0", "sha256": "0" * 64}, "exactly when"),
    ],
)
def test_plan_preset_matches_mode(mode, preset, message):
    record = _plan_record()
    record["plan"].update(mode=mode, preset=preset)
    assert any(message in e for e in _plan_errors(record))


def test_chosen_by_preset_needs_a_preset_plan():
    record = _plan_record()
    record["plan"].update(mode="free", preset=None)
    assert any("chosen_by 'preset'" in e for e in _plan_errors(record))


def test_free_plan_needs_targets():
    record = _plan_record()
    for slot in record["slots"]:
        if slot.get("implementation", {}).get("chosen_by") == "preset":
            slot["implementation"]["chosen_by"] = "user"
    record["plan"].update(mode="free", preset=None)
    assert _plan_errors(record) == []
    record["plan"]["targets"] = {}
    assert any("at least one target" in e for e in _plan_errors(record))


def test_plan_roles_of_slots():
    record = _plan_record()
    _slot(record, "report").pop("plan_role")
    assert any("needs plan_role" in e for e in _plan_errors(record))
    record = _plan_record()
    _slot(record, "cognitive-profile")["plan_role"] = "upstream"
    assert any("only an 'if_available' slot" in e for e in _plan_errors(record))
    record = _plan_record()
    _slot(record, "normalisation")["plan_role"] = "upstream"
    assert any("has no plan_role" in e for e in _plan_errors(record))


def test_target_both_required_and_if_available():
    record = _plan_record()
    record["plan"]["targets"]["if_available"].append("report.report_pdf")
    assert any("both required and if_available" in e for e in _plan_errors(record))


def test_plan_targets_are_checked_against_the_pipeline():
    record = _plan_record()
    assert _cross_errors(("pipeline.yaml", _pipeline()), ("p.json", record)) == []
    record["plan"]["targets"]["required"].append("report.summary_pdf")
    _slot(record, "functional-disconnection")["plan_role"] = "upstream"
    errors = _cross_errors(("pipeline.yaml", _pipeline()), ("p.json", record))
    assert any("'report.summary_pdf' is not a port" in e for e in errors)
    assert any("must have plan_role 'target'" in e for e in errors)


def _segmenter_pipeline():
    """The worked pipeline plus a segmentation slot filled by the wrapper example."""
    pipeline = _pipeline()
    pipeline["slots"].insert(
        0,
        {
            "name": "segmentation",
            "optional": True,
            "consumes": [{"port": "ct", "type": "ct_native", "from": "inputs.ct"}],
            "produces": [{"port": "lesion_mask_native", "type": "lesion_mask_native"}],
            "implementations": {
                "allowed": [
                    {
                        "id": "example-segmenter",
                        "step": {
                            "name": "lesion-segmenter-wrapper",
                            "version": "0.1.0",
                        },
                        "image": "docker.io/compneurobilbaolab/step-lesion-segmenter-wrapper@sha256:"
                        + "1" * 64,
                    }
                ]
            },
        },
    )
    return pipeline


def test_slot_record_upstream_is_a_copy_of_the_manifest_block():
    step = _load("step-lesion-segmenter-wrapper.yaml")
    pipeline = _segmenter_pipeline()
    impl = pipeline["slots"][0]["implementations"]["allowed"][0]
    record = _plan_record()
    record["slots"].insert(
        0,
        {
            "slot": "segmentation",
            "status": "selected",
            "plan_role": "if_available",
            "implementation": {
                **impl,
                "stub": False,
                "chosen_by": "user",
                "upstream": copy.deepcopy(step["upstream"]),
            },
            "bundles": [],
        },
    )
    docs = [("step.yaml", step), ("pipeline.yaml", pipeline), ("p.json", record)]
    assert _cross_errors(*docs) == []
    record["slots"][0]["implementation"]["upstream"]["version"] = "2.2.0"
    assert any("differs from the step manifest" in e for e in _cross_errors(*docs))
    del record["slots"][0]["implementation"]["upstream"]
    assert any("must copy" in e for e in _cross_errors(*docs))


# Presets: single-document rules.


def test_preset_schema_is_registered():
    assert v.SCHEMA_FILES["preset"] == "preset.schema.json"
    assert v.detect_schema(_preset(), Path("anything.yaml")) == "preset"


@pytest.mark.parametrize(
    ("change", "ok"),
    [
        ({"input_sets": []}, False),
        ({"input_sets": ["mni-mask", "mni-mask"]}, False),
        ({"targets": {"if_available": ["report"]}}, False),  # required is required
        ({"targets": {"required": []}}, False),
        ({"targets": {"required": ["Report"]}}, False),
        ({"targets": {"required": ["report.report_pdf", "lesion_qc"]}}, True),
        ({"targets": {"required": ["report.report-pdf"]}}, False),
        ({"pipeline": {"id": "stroke-lnm", "versions": ">=0.1.0 <0.2.0"}}, False),
        ({"pipeline": {"id": "stroke-lnm", "versions": "^0.1.0"}}, True),
        ({"implementations": {"report": {}}}, False),
        ({"implementations": {"report": {"preferred": "research-report"}}}, False),
        ({"slots": []}, False),  # no slot or wiring in a preset
    ],
)
def test_preset_schema(change, ok):
    assert (v.schema_errors(_preset(**change), "preset") == []) is ok


def test_preset_rule_p1_contract_version():
    assert v.check_preset(_preset()) == []
    for version in ("1.0", "1.1"):
        assert any(
            "(P1)" in e for e in v.check_preset(_preset(contract_version=version))
        )


def test_preset_rule_p7_literal_targets():
    preset = _preset()
    preset["targets"]["if_available"].append("report")
    assert any("(P7)" in e for e in v.check_preset(preset))


# Presets against a pipeline.


def _preset_errors(preset, pipeline=None, manifests=None):
    return v.preset_errors(preset, pipeline or _pipeline(), manifests)


def test_preset_example_is_valid_against_the_pipeline():
    assert _preset_errors(_preset()) == []


def test_preset_rule_p2_attachment(tmp_path):
    def errors(*pipelines, strict=False, preset=None):
        docs = [("preset.yaml", preset or _preset())]
        docs += [(f"pipeline-{p['version']}.yaml", p) for p in pipelines]
        found = [v.Document(Path(n), d, v.detect_schema(d, Path(n))) for n, d in docs]
        return v.cross_check(found, strict=strict)

    # Attached to 0.1.0 only; 0.2.0 is outside ">=0.1.0, <0.2.0" and not checked.
    broken = _pipeline("0.2.0")
    broken["input_sets"] = [s for s in broken["input_sets"] if s["name"] != "mni-mask"]
    assert _errors(errors(_pipeline(), broken)) == []
    # Inside the range, each version is validated separately.
    wide = _preset()
    wide["pipeline"]["versions"] = ">=0.1.0, <1.0.0"
    found = _errors(errors(_pipeline(), broken, preset=wide))
    assert found and all("pipeline stroke-lnm 0.2.0" in i.message for i in found)
    # No supplied version in the range: a note, an error with --strict.
    issues = errors(broken)
    assert not _errors(issues) and any("P2-P7 not checked" in i.message for i in issues)
    assert _errors(errors(broken, strict=True))
    assert any("not checked" in i.message for i in errors())


def test_preset_rule_p3_input_sets():
    errors = _preset_errors(_preset(input_sets=["mni-mask", "t1w-only"]))
    assert any("'t1w-only' is not an input set" in e and "(P3)" in e for e in errors)


@pytest.mark.parametrize(
    ("required", "code"),
    [
        (["segmentation"], "unknown_target"),
        (["report.summary_pdf"], "unknown_target"),
        (["qc_figure"], "ambiguous_target"),
        (["normalisation"], "target_not_applicable"),
        (["lesion-intake.qc_figure"], "target_not_guaranteed"),
    ],
)
def test_preset_rule_p4_targets_resolve(required, code):
    preset = _preset(targets={"required": required})
    preset.pop("implementations")
    errors = _preset_errors(preset)
    assert any(f"[{code}]" in e and "(P4)" in e for e in errors), errors


def test_preset_rule_p4_if_available_target_must_apply():
    preset = _preset(
        targets={"required": ["report"], "if_available": ["normalisation"]}
    )
    errors = _preset_errors(preset)
    assert any("if-available target 'normalisation'" in e for e in errors)
    # Not guaranteed is fine as if-available.
    preset["targets"]["if_available"] = ["lesion-intake.qc_figure"]
    assert _preset_errors(preset) == []


def test_preset_rule_p5_feasibility():
    pipeline = _unreachable_pipeline()
    preset = _preset(targets={"required": ["cognitive-profile"]})
    preset.pop("implementations")
    errors = _preset_errors(preset, pipeline)
    assert any("[target_unreachable]" in e and "(P5)" in e for e in errors)


def test_preset_rule_p5_assumes_every_allowed_implementation_enabled():
    pipeline = _pipeline()
    fd = next(s for s in pipeline["slots"] if s["name"] == "functional-disconnection")
    assert "default" not in fd["implementations"]  # no default, still feasible
    assert _preset_errors(_preset(), pipeline) == []


@pytest.mark.parametrize(
    ("implementations", "message"),
    [
        ({"segmentation": {"default": "x"}}, "not a slot of the pipeline"),
        ({"normalisation": {"default": "ct-lesion-to-mni152"}}, "not in the plan"),
        ({"report": {"fixed": "another-report"}}, "not an allowed implementation"),
    ],
)
def test_preset_rule_p6_implementations(implementations, message):
    errors = _preset_errors(_preset(implementations=implementations))
    assert any(message in e and "(P6)" in e for e in errors), errors


def test_preset_rule_p6_fixed_stub():
    stub = _load("step-fdisconnection-stub.yaml")
    preset = _preset(
        implementations={"functional-disconnection": {"fixed": "fdisconnection-stub"}}
    )
    manifests = {("fdisconnection-stub", "0.1.0"): stub}
    assert any(
        "stub cannot be a fixed choice" in e
        for e in _preset_errors(preset, None, manifests)
    )
    preset["implementations"]["functional-disconnection"] = {
        "default": "fdisconnection-stub"
    }
    assert _preset_errors(preset, None, manifests) == []
    # Without the manifest the cross-check says so (an error with --strict).
    preset["implementations"]["functional-disconnection"] = {
        "fixed": "fdisconnection-stub"
    }
    docs = [
        v.Document(Path("preset.yaml"), preset, "preset"),
        v.Document(Path("pipeline.yaml"), _pipeline(), "pipeline"),
    ]
    issues = v.cross_check(docs, strict=False)
    assert any("stub not checked" in i.message for i in issues) and not _errors(issues)
    assert _errors(v.cross_check(docs, strict=True))


def test_preset_rule_p7_resolved_slots():
    preset = _preset(
        targets={
            "required": ["report.report_pdf"],
            "if_available": ["report.report_json"],
        }
    )
    assert v.check_preset(preset) == []  # different strings, same slot
    assert any("(P7)" in e for e in _preset_errors(preset))


def test_preset_rule_p8_one_file_per_version(tmp_path):
    text = (EXAMPLES / "preset-mni-mask-analysis.yaml").read_text(encoding="utf-8")
    a, b, c = tmp_path / "a.yaml", tmp_path / "b.yaml", tmp_path / "c.yaml"
    a.write_text(text, encoding="utf-8")
    b.write_text(text, encoding="utf-8")
    c.write_text(
        text.replace("title: Disconnection", "title: Changed"), encoding="utf-8"
    )
    pipeline = EXAMPLES / "pipeline-stroke-lnm.yaml"
    assert _errors(v.validate_files([a, pipeline])) == []
    same = [i.message for i in _errors(v.validate_files([a, b, pipeline]))]
    assert any("(P8)" in m and "different content" not in m for m in same)
    changed = [i.message for i in _errors(v.validate_files([a, c, pipeline]))]
    assert any("(P8)" in m and "different content" in m for m in changed)
    # Another version of the same preset is fine.
    c.write_text(text.replace("version: 1.0.0", "version: 1.1.0"), encoding="utf-8")
    assert _errors(v.validate_files([a, c, pipeline])) == []
    assert v.preset_sha256(a.read_bytes()) == v.preset_sha256(b.read_bytes())


# Static plan closure: refusal codes.


def _unreachable_pipeline():
    """A pipeline that fails the static check: cognitive-profile reads a port that
    no slot can produce under mni-mask. Only for the pure closure function."""
    pipeline = _pipeline()
    cp = next(s for s in pipeline["slots"] if s["name"] == "cognitive-profile")
    cp["consumes"].append(
        {"port": "ct_mni", "type": "ct_mni", "from": "normalisation.ct_mni"}
    )
    cp["when"] = {"inputs_present": ["lesion_mni"]}
    return pipeline


def _codes(plan):
    return [r.code for r in plan.reasons]


@pytest.mark.parametrize(
    ("required", "if_available", "code"),
    [
        (["segmentation"], None, "unknown_target"),
        (None, ["segmentation"], "unknown_target"),
        (["normalisation.ct_native"], None, "unknown_target"),
        (["qc_figure"], None, "ambiguous_target"),
        (None, ["qc_figure"], "ambiguous_target"),
        (["normalisation"], None, "target_not_applicable"),
        (["normalisation.lesion_mask_mni"], None, "target_not_applicable"),
        (["lesion-intake.qc_figure"], None, "target_not_guaranteed"),
    ],
)
def test_closure_resolution_refusals(required, if_available, code):
    plan = v.plan_closure(_pipeline(), "mni-mask", required, if_available)
    assert not plan.feasible and _codes(plan) == [code]
    assert plan.reasons[0].message and plan.reasons[0].target


def test_ambiguous_target_lists_candidates():
    plan = v.plan_closure(_pipeline(), "mni-mask", ["qc_figure"])
    assert "lesion-intake.qc_figure" in plan.reasons[0].message
    assert "cognitive-profile.qc_figure" in plan.reasons[0].message


def test_artefact_type_target_with_one_candidate():
    plan = v.plan_closure(_pipeline(), "mni-mask", ["tract_disconnection"])
    assert plan.feasible
    assert plan.targets == {"required": ["tract-disconnection.tract_disconnection"]}
    # Under mni-mask only intake produces the MNI mask; normalisation is never run.
    plan = v.plan_closure(_pipeline(), "mni-mask", ["lesion_mask_mni"])
    assert plan.targets == {"required": ["lesion-intake.lesion_mask_mni"]}


def test_slot_name_that_is_also_an_artefact_type_is_ambiguous():
    pipeline = _pipeline()
    pipeline["slots"][0]["name"] = "lesion"
    for slot in pipeline["slots"]:
        for c in slot["consumes"]:
            sources = c["from"] if isinstance(c["from"], list) else [c["from"]]
            c["from"] = [s.replace("lesion-intake.", "lesion.") for s in sources]
    pipeline["slots"][0]["produces"].append(
        {"port": "extra", "type": "lesion", "optional": True}
    )
    types = {**_types(), "lesion": _types()["lesion_qc"]}
    assert v.check_pipeline(pipeline, types) == []
    plan = v.plan_closure(pipeline, "mni-mask", ["lesion"])
    assert _codes(plan) == ["ambiguous_target"]


def test_closure_target_unreachable():
    plan = v.plan_closure(_unreachable_pipeline(), "mni-mask", ["cognitive-profile"])
    assert _codes(plan) == ["target_unreachable"]
    reason = plan.reasons[0]
    assert (reason.target, reason.slot, reason.port) == (
        "cognitive-profile",
        "cognitive-profile",
        "ct_mni",
    )
    # As if-available it does not refuse: the slot is skipped.
    plan = v.plan_closure(
        _unreachable_pipeline(), "mni-mask", ["report"], ["cognitive-profile"]
    )
    assert plan.feasible
    assert plan.slot("cognitive-profile").reason == "upstream_absent"


def test_closure_no_implementation_names_the_target_that_needed_it():
    enabled = {"lesion-intake": ["lesion-qc"], "report": ["research-report"]}
    plan = v.plan_closure(_pipeline(), "mni-mask", ["report"], None, enabled)
    assert _codes(plan) == ["no_implementation"]
    reason = plan.reasons[0]
    assert (reason.slot, reason.required_by, reason.target) == (
        "tract-disconnection",
        "report",
        "report",
    )
    assert plan.slot("tract-disconnection").status == "unavailable"


def test_closure_collects_every_reason():
    plan = v.plan_closure(
        _pipeline(), "mni-mask", ["normalisation", "segmentation", "report"], None, {}
    )
    assert sorted(set(_codes(plan))) == [
        "no_implementation",
        "target_not_applicable",
        "unknown_target",
    ]
    assert _codes(plan).count("no_implementation") == 3  # report, intake, tract


def test_closure_unknown_input_set():
    with pytest.raises(ValueError):
        v.plan_closure(_pipeline(), "t1w-only")


# Static plan closure: plans.


def _statuses(plan):
    return {s.slot: (s.status, s.reason, s.plan_role) for s in plan.slots}


ALL = None  # every allowed implementation enabled


def test_r0_reproduces_the_run_resolution_of_pipeline_md_6():
    plan = v.plan_closure(_pipeline(), "mni-mask")
    assert plan.feasible and plan.targets == {}
    assert _statuses(plan) == {
        "lesion-intake": ("planned", None, "target"),
        "normalisation": ("skipped", "condition_false", None),
        "functional-disconnection": ("planned", None, "if_available"),
        "tract-disconnection": ("planned", None, "target"),
        "cognitive-profile": ("planned", None, "if_available"),
        "report": ("planned", None, "target"),
    }
    # An optional slot without an enabled implementation is unavailable and the
    # optional slots that need it are skipped; the run is not refused.
    enabled = {
        "lesion-intake": ["lesion-qc"],
        "tract-disconnection": ["lnm-tracts"],
        "cognitive-profile": ["cognet-stroke"],
        "report": ["research-report"],
    }
    plan = v.plan_closure(_pipeline(), "mni-mask", enabled=enabled)
    assert plan.feasible
    assert plan.slot("functional-disconnection").reason == "no_implementation"
    assert plan.slot("cognitive-profile").reason == "upstream_absent"
    # A non-optional slot without one refuses the run.
    del enabled["tract-disconnection"]
    assert _codes(v.plan_closure(_pipeline(), "mni-mask", enabled=enabled)) == [
        "no_implementation"
    ]


def test_first_qualifying_source_is_used():
    plan = v.plan_closure(_pipeline(), "ct-and-native-mask", ["tract-disconnection"])
    assert plan.planned() == {
        "normalisation": "upstream",
        "tract-disconnection": "target",
    }
    assert plan.slot("lesion-intake").reason == "not_requested"
    plan = v.plan_closure(_pipeline(), "mni-mask", ["tract-disconnection"])
    assert plan.planned() == {
        "lesion-intake": "upstream",
        "tract-disconnection": "target",
    }


def test_optional_consumed_ports_never_pull_slots_in():
    plan = v.plan_closure(_pipeline("0.2.0"), "mni-mask", ["report"])
    assert plan.planned() == {"lesion-intake": "upstream", "report": "target"}
    assert plan.slot("tract-disconnection").reason == "not_requested"


def test_required_target_on_an_optional_slot_makes_it_required():
    plan = v.plan_closure(_pipeline(), "mni-mask", ["cognitive-profile"])
    assert plan.planned() == {
        "lesion-intake": "upstream",
        "functional-disconnection": "upstream",
        "cognitive-profile": "target",
    }
    enabled = {"lesion-intake": ["lesion-qc"], "cognitive-profile": ["cognet-stroke"]}
    plan = v.plan_closure(_pipeline(), "mni-mask", ["cognitive-profile"], None, enabled)
    assert _codes(plan) == ["no_implementation"]
    assert plan.reasons[0].required_by == "cognitive-profile"


def test_plan_closure_is_pure():
    pipeline = _pipeline()
    before = copy.deepcopy(pipeline)
    required, optional = ["report"], ["cognitive-profile"]
    first = v.plan_closure(pipeline, "mni-mask", required, optional)
    assert v.plan_closure(pipeline, "mni-mask", required, optional) == first
    assert pipeline == before and required == ["report"]


# The four stroke-lnm presets of design run-plans.md 2.5 (written for the worked pipeline,
# whose COGNET slot is named cognitive-profile).

STROKE_LNM_PRESETS = {
    "full": {
        "input_sets": ["ct-and-native-mask", "mni-mask"],
        "versions": ">=0.1.0, <1.0.0",
        "targets": {
            "required": ["report"],
            "if_available": ["functional-disconnection", "cognitive-profile"],
        },
    },
    "mni-mask-analysis": {
        "input_sets": ["mni-mask"],
        "versions": ">=0.1.0, <0.2.0",
        "targets": {
            "required": ["tract-disconnection", "functional-disconnection", "report"],
            "if_available": ["cognitive-profile"],
        },
    },
    "ct-normalisation": {
        "input_sets": ["ct-and-native-mask"],
        "versions": ">=0.1.0, <1.0.0",
        "targets": {"required": ["lesion-intake", "normalisation"]},
    },
    "lesion-qc-report": {
        "input_sets": ["ct-and-native-mask", "mni-mask"],
        "versions": ">=0.2.0, <0.3.0",
        "targets": {"required": ["lesion-intake", "report"]},
    },
}

U, T, OPT = "upstream", "target", "if_available"
NR, CF = "not_requested", "condition_false"
SLOT_ORDER = [
    "lesion-intake",
    "normalisation",
    "functional-disconnection",
    "tract-disconnection",
    "cognitive-profile",
    "report",
]


def _row(*cells):
    """Expected (status, reason, plan_role) per slot, in pipeline order."""
    out = {}
    for name, cell in zip(SLOT_ORDER, cells):
        out[name] = (
            ("skipped", cell, None) if cell in (NR, CF) else ("planned", None, cell)
        )
    return out


EXPECTED_PLANS = {
    ("full", "0.1.0", "mni-mask"): _row(U, CF, OPT, U, OPT, T),
    ("full", "0.1.0", "ct-and-native-mask"): _row(U, U, OPT, U, OPT, T),
    ("mni-mask-analysis", "0.1.0", "mni-mask"): _row(U, CF, T, T, OPT, T),
    ("ct-normalisation", "0.1.0", "ct-and-native-mask"): _row(T, T, NR, NR, NR, NR),
    # In 0.1.0 the report still needs the tract table (no preset attaches there; the
    # closure shows why the pipeline change of card F1-26 is needed).
    ("lesion-qc-report", "0.1.0", "mni-mask"): _row(T, CF, NR, U, NR, T),
    ("lesion-qc-report", "0.1.0", "ct-and-native-mask"): _row(T, U, NR, U, NR, T),
    ("lesion-qc-report", "0.2.0", "mni-mask"): _row(T, CF, NR, NR, NR, T),
    ("lesion-qc-report", "0.2.0", "ct-and-native-mask"): _row(T, U, NR, NR, NR, T),
}


def _stroke_lnm_preset(name):
    spec = STROKE_LNM_PRESETS[name]
    return {
        "contract_version": "1.2",
        "kind": "Preset",
        "id": name,
        "version": "1.0.0",
        "title": name,
        "pipeline": {"id": "stroke-lnm", "versions": spec["versions"]},
        "input_sets": spec["input_sets"],
        "targets": spec["targets"],
    }


@pytest.mark.parametrize("key", sorted(EXPECTED_PLANS), ids="-".join)
def test_stroke_lnm_presets_give_the_plans_of_the_design(key):
    name, version, input_set = key
    preset = _stroke_lnm_preset(name)
    assert v.schema_errors(preset, "preset") == [] and v.check_preset(preset) == []
    targets = preset["targets"]
    plan = v.plan_closure(
        _pipeline(version), input_set, targets["required"], targets.get("if_available")
    )
    assert plan.feasible, plan.reasons
    assert _statuses(plan) == EXPECTED_PLANS[key]


@pytest.mark.parametrize("name", sorted(STROKE_LNM_PRESETS))
def test_stroke_lnm_presets_are_valid_for_the_versions_they_attach_to(name):
    preset = _stroke_lnm_preset(name)
    docs = [v.Document(Path(f"{name}.yaml"), preset, "preset")]
    for version in ("0.1.0", "0.2.0"):
        docs.append(
            v.Document(Path(f"p-{version}.yaml"), _pipeline(version), "pipeline")
        )
    issues = v.cross_check(docs, strict=False)
    assert _errors(issues) == []
    assert not any("P2-P7 not checked" in i.message for i in issues)


def _same_statuses(pipeline, required, optional):
    deployment = {
        "lesion-intake": ["lesion-qc"],
        "normalisation": ["ct-lesion-to-mni152"],
        "tract-disconnection": ["lnm-tracts"],
        "report": ["research-report"],
    }
    for input_set in ("ct-and-native-mask", "mni-mask"):
        for enabled in (ALL, deployment):
            full = v.plan_closure(pipeline, input_set, enabled=enabled)
            preset = v.plan_closure(pipeline, input_set, required, optional, enabled)
            assert full.feasible and preset.feasible
            if [(s.slot, s.status, s.reason) for s in full.slots] != [
                (s.slot, s.status, s.reason) for s in preset.slots
            ]:
                return False
    return True


def test_full_preset_equals_the_full_plan_except_roles():
    targets = STROKE_LNM_PRESETS["full"]["targets"]
    required, optional = targets["required"], targets["if_available"]
    assert _same_statuses(_pipeline("0.1.0"), required, optional)
    # Once the report's tract table is optional (0.2.0), "report" alone no longer pulls
    # tract disconnection in: a full preset for 0.2.0 must name it (card F1-20).
    assert not _same_statuses(_pipeline("0.2.0"), required, optional)
    assert _same_statuses(
        _pipeline("0.2.0"), required + ["tract-disconnection"], optional
    )


def test_presets_refused_or_unavailable_in_a_deployment():
    # ct-normalisation is offered as unavailable while normalisation is not enabled.
    enabled = {
        "lesion-intake": ["lesion-qc"],
        "report": ["research-report"],
        "tract-disconnection": ["lnm-tracts"],
    }
    targets = STROKE_LNM_PRESETS["ct-normalisation"]["targets"]
    plan = v.plan_closure(
        _pipeline(), "ct-and-native-mask", targets["required"], None, enabled
    )
    assert [(r.code, r.slot) for r in plan.reasons] == [
        ("no_implementation", "normalisation")
    ]
    # mni-mask-analysis is refused without functional disconnection, unlike full.
    targets = STROKE_LNM_PRESETS["mni-mask-analysis"]["targets"]
    plan = v.plan_closure(
        _pipeline(), "mni-mask", targets["required"], targets["if_available"], enabled
    )
    assert [(r.code, r.slot) for r in plan.reasons] == [
        ("no_implementation", "functional-disconnection")
    ]
    targets = STROKE_LNM_PRESETS["full"]["targets"]
    plan = v.plan_closure(
        _pipeline(), "mni-mask", targets["required"], targets["if_available"], enabled
    )
    assert plan.feasible
    assert plan.slot("functional-disconnection").status == "unavailable"


def test_worked_examples_agree():
    """The plan record example is the plan of the preset example (cognitive-profile
    has no enabled implementation in that deployment)."""
    preset, record = _preset(), _plan_record()
    enabled = {
        s["slot"]: [s["implementation"]["id"]]
        for s in record["slots"]
        if s["status"] == "selected"
    }
    plan = v.plan_closure(
        _pipeline(),
        record["input_set"],
        preset["targets"]["required"],
        preset["targets"]["if_available"],
        enabled,
    )
    assert plan.feasible
    assert plan.targets == record["plan"]["targets"]
    for slot in record["slots"]:
        planned = plan.slot(slot["slot"])
        assert planned.plan_role == slot.get("plan_role"), slot["slot"]
        assert planned.reason == slot.get("reason"), slot["slot"]
    assert record["plan"]["preset"]["id"] == preset["id"]
    assert record["plan"]["preset"]["version"] == preset["version"]


def test_normative_text_lists_every_rule_and_code():
    text = (ROOT / "docs/contracts/presets.md").read_text(encoding="utf-8")
    for rule in ("P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8"):
        assert f"| {rule} |" in text, rule
    for code in v.RESOLUTION_CODES + v.CLOSURE_CODES:
        assert f"`{code}`" in text, code
    history = (ROOT / "docs/contracts/versioning.md").read_text(encoding="utf-8")
    assert (
        "| `1.2` |" in history and "0018-presets-free-mode-external-tools.md" in history
    )
