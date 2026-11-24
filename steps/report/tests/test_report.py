"""Tests of the research-report step.

Golden files (tests/golden/) hold the expected report.json byte for byte and the expected
PDF text (never PDF bytes). Regenerate them after an intended change with

    REPORT_UPDATE_GOLDEN=1 python -m pytest steps/report

REPORT_ACTUAL_DIR=<dir> additionally keeps the PDFs, texts and JSON files of the test runs
(CI uploads them as an artefact). PDF tests are skipped where WeasyPrint's system
libraries (Pango) are missing; CI has them.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from jsonschema import Draft202012Validator

STEP_DIR = Path(__file__).resolve().parents[1]
REPO = STEP_DIR.parents[1]
sys.path.insert(0, str(STEP_DIR / "src"))
sys.path.insert(0, str(STEP_DIR / "tests"))

# loaded under a unique name: other steps also have a tests/make_reference.py
_spec = importlib.util.spec_from_file_location(
    "report_make_reference", STEP_DIR / "tests" / "make_reference.py"
)
make_reference = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_reference)

import report_pdftext  # noqa: E402
from research_report_step import canonical, model, render  # noqa: E402
from research_report_step import run as step_run  # noqa: E402

from stroke_step_sdk import EXIT_OK, EXIT_REJECTED, reftest, run_step  # noqa: E402

MANIFEST = STEP_DIR / "step.yaml"
GOLDEN = STEP_DIR / "tests" / "golden"
UPDATE = os.environ.get("REPORT_UPDATE_GOLDEN") == "1"
ACTUAL = os.environ.get("REPORT_ACTUAL_DIR")
SCHEMA = Draft202012Validator(
    json.loads((STEP_DIR / "report.schema.json").read_text(encoding="utf-8"))
)
FILES = {
    "lesion_qc": "lesion_qc.json",
    "lesion_mask": "lesion_mask.nii.gz",
    "run_provenance": "run_provenance.json",
    "fdisconnection_map": "fdisconnection_map.nii.gz",
    "tract_disconnection": "tract_disconnection.tsv",
    "cognet_profile": "cognet_profile.json",
}
MINIMAL = ("lesion_qc", "lesion_mask", "run_provenance")
DATA = STEP_DIR / "tests" / "data"
# run records of contract 1.2 (generated from the 1.0 record, validated with contracts/validate.py)
PRESET_RUN = "run_provenance_preset.json"  # preset mni-mask-analysis, COGNET if available but absent
CUSTOM_RUN = "run_provenance_custom.json"  # free plan: lesion QC and report; three slots not requested
EXTERNAL_RUN = "run_provenance_external.json"  # full plan, external tool, declared mask origin
NO_COGNET = tuple(p for p in FILES if p != "cognet_profile")
NO_TRACT = tuple(p for p in FILES if p != "tract_disconnection")  # QC report without tracts


def _pdf_available() -> bool:
    try:
        import weasyprint  # noqa: F401
    except (ImportError, OSError):
        return False
    return True


needs_pdf = pytest.mark.skipif(not _pdf_available(), reason="WeasyPrint system libraries missing")


@pytest.fixture(scope="session")
def reference(tmp_path_factory) -> Path:
    dest = tmp_path_factory.mktemp("reference")
    make_reference.generate(dest)
    return dest


class Staged:
    def __init__(self, root: Path, reference: Path, ports=tuple(FILES), language="en") -> None:
        self.in_dir, self.out_dir = root / "in", root / "out"
        for d in (self.in_dir, self.out_dir, root / "tmp"):
            d.mkdir()
        self.env = {"STEP_CPUS": "1", "TMPDIR": str(root / "tmp")}
        self.params = {"language": language}
        for port in ports:
            self.put(port, reference / FILES[port])

    def with_run_record(self, name: str) -> "Staged":
        """Replace the staged run record by a committed one (CRLF checkouts must not change it)."""
        (self.in_dir / "run_provenance.json").write_bytes(
            (DATA / name).read_bytes().replace(b"\r\n", b"\n")
        )
        return self

    def without_tract_slot(self) -> "Staged":
        """Provenance of a QC-only plan: the tract slot has no enabled implementation."""
        path = self.in_dir / "run_provenance.json"
        doc = json.loads(path.read_text(encoding="utf-8"))
        for slot in doc["slots"]:
            if slot["slot"] == "tract-disconnection":
                for key in [k for k in slot if k not in ("slot",)]:
                    del slot[key]
                slot.update(status="unavailable", reason="no_implementation")
        doc["steps"] = [s for s in doc["steps"] if s["slot"] != "tract-disconnection"]
        path.write_text(json.dumps(doc), encoding="utf-8")
        return self

    def put(self, port: str, source: Path) -> None:
        shutil.copyfile(source, self.in_dir / f"{port}{''.join(Path(FILES[port]).suffixes)}")

    def run(self) -> int:
        (self.in_dir / "params.json").write_text(json.dumps(self.params), encoding="utf-8")
        return run_step(
            step_run, self.in_dir, self.out_dir, manifest_path=MANIFEST, environ=self.env
        )

    def json_bytes(self) -> bytes:
        return (self.out_dir / "report_json.json").read_bytes()

    def error(self) -> dict:
        return json.loads((self.out_dir / "error.json").read_text(encoding="utf-8"))


@pytest.fixture
def no_pdf(monkeypatch):
    """Skip WeasyPrint (report.json tests need neither Pango nor fonts)."""
    monkeypatch.setattr(
        render, "write_pdf", lambda html, out: Path(out).write_bytes(b"%PDF-1.7\n%stub\n")
    )


def _check_golden_bytes(name: str, actual: bytes) -> None:
    path = GOLDEN / name
    if ACTUAL:
        Path(ACTUAL).mkdir(parents=True, exist_ok=True)
        (Path(ACTUAL) / name).write_bytes(actual)
    if UPDATE:
        path.write_bytes(actual)
    assert path.read_bytes() == actual, f"report.json differs from {name}"


# --- canonical JSON ----------------------------------------------------------------


def test_canonical_floats_and_order():
    doc = {"b": [1.0, 0.1 + 0.2, -0.0, 1e-9, 2.5e-5, 123456789.125, True, None], "a": "é"}
    assert canonical.dumps(doc) == (
        r'{"a":"\u00e9","b":[1.0,0.3,0.0,0.0,0.000025,123456789.125,true,null]}'
    )


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_canonical_rejects_non_finite(bad):
    with pytest.raises(ValueError):
        canonical.dumps({"x": bad})


def test_canonical_is_reparseable_and_stable():
    doc = {"z": {"k": [0.5, 2, "x"]}, "a": False}
    text = canonical.dumps(doc)
    assert json.loads(text) == doc
    assert canonical.dumps(json.loads(text)) == text


# --- report.json (golden, byte for byte) --------------------------------------------------


def test_report_json_golden_full_en(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference)
    assert staged.run() == EXIT_OK
    data = staged.json_bytes()
    _check_golden_bytes("report_full_en.json", data)
    doc = json.loads(data)
    SCHEMA.validate(doc)
    assert doc["sections"] == {
        "lesion": "available",
        "functional_disconnection": "available",
        "tract_disconnection": "available",
        "cognitive_profile": "available",
    }
    assert not data.endswith(b"\n")
    assert b"accounting" not in data and b"submitted_by" not in data


def test_report_json_golden_without_tract_table_en(tmp_path, reference, no_pdf):
    """The tract section of a run without tract disconnection (QC-only plan) is absent, not an error."""
    staged = Staged(tmp_path, reference, ports=NO_TRACT).without_tract_slot()
    assert staged.run() == EXIT_OK
    data = staged.json_bytes()
    _check_golden_bytes("report_notract_en.json", data)
    doc = json.loads(data)
    SCHEMA.validate(doc)
    assert doc["sections"] == {
        "lesion": "available",
        "functional_disconnection": "available",
        "tract_disconnection": "absent",
        "cognitive_profile": "available",
    }
    assert doc["tract_disconnection"] == {"status": "absent"}
    assert doc["generator"] == {"step": "research-report", "version": "0.4.0"}
    absent = [i["port"] for i in doc["inputs"] if not i["present"]]
    assert absent == ["ct_mni", "tract_disconnection"]


def test_tract_table_only_changes_the_tract_parts_of_the_report(tmp_path, reference, no_pdf):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with_tracts = Staged(tmp_path / "a", reference)
    without = Staged(tmp_path / "b", reference, ports=NO_TRACT)  # same provenance
    assert with_tracts.run() == EXIT_OK and without.run() == EXIT_OK
    a, b = json.loads(with_tracts.json_bytes()), json.loads(without.json_bytes())
    assert a["tract_disconnection"]["status"] == "available"
    for doc in (a, b):
        doc.pop("tract_disconnection")
        doc["sections"].pop("tract_disconnection")
        doc["inputs"] = [i for i in doc["inputs"] if i["port"] != "tract_disconnection"]
    assert a == b


# --- plan, external tools, declared origins (run records of contract 1.2) -----------------------


def _assert_no_plan_keys(doc: dict) -> None:
    for key in ("plan", "external_tools", "declared_origins"):
        assert key not in doc
    assert not {"external_tool_used", "input_origin_declared_by_user"} & set(doc["notes"])


def test_record_without_plan_gives_the_content_of_report_1_0(tmp_path, reference, no_pdf):
    """Full-plan output is unchanged apart from the version fields: a 1.0 record adds no key."""
    staged = Staged(tmp_path, reference)
    assert staged.run() == EXIT_OK
    doc = json.loads(staged.json_bytes())
    _assert_no_plan_keys(doc)
    assert doc["report_version"] == "1.2" and doc["generator"]["version"] == "0.4.0"
    assert set(doc) == {
        "kind", "report_version", "language", "research_use_only", "research_use_statement",
        "generator", "sections", "notes", "lesion", "functional_disconnection",
        "tract_disconnection", "cognitive_profile", "provenance", "inputs",
    }  # fmt: skip


def test_report_json_golden_preset_run_en(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference, ports=NO_COGNET).with_run_record(PRESET_RUN)
    assert staged.run() == EXIT_OK
    data = staged.json_bytes()
    _check_golden_bytes("report_preset_en.json", data)
    doc = json.loads(data)
    SCHEMA.validate(doc)
    assert doc["plan"] == {
        "mode": "preset",
        "preset": {
            "id": "mni-mask-analysis",
            "version": "1.1.0",
            "sha256": "8459de0e342a20033358dd3827bffd5893163fff2b64415f69d3ae5262edda56",
        },
        "targets": {
            "required": [
                "tract-disconnection.tract_disconnection",
                "functional-disconnection.fdisconnection_map",
                "report.report_pdf",
                "report.report_json",
            ],
            "if_available": ["cognet-profile.profile"],
        },
    }
    # the COGNET slot was planned "if available" but has no implementation: absent, not "not requested"
    assert doc["cognitive_profile"] == {"status": "absent"}
    roles = {s["slot"]: s.get("plan_role") for s in doc["provenance"]["slots"]}
    assert roles["lesion-intake"] == "upstream" and roles["report"] == "target"
    assert roles["cognet-profile"] == "if_available" and roles["normalisation"] is None
    assert "external_tools" not in doc and "declared_origins" not in doc


def test_report_json_golden_custom_run_es(tmp_path, reference, no_pdf):
    """A free plan with lesion QC and the report only: the three analyses are not requested."""
    staged = Staged(tmp_path, reference, ports=MINIMAL, language="es").with_run_record(CUSTOM_RUN)
    assert staged.run() == EXIT_OK
    data = staged.json_bytes()
    _check_golden_bytes("report_custom_es.json", data)
    doc = json.loads(data)
    SCHEMA.validate(doc)
    assert doc["plan"]["mode"] == "free" and doc["plan"]["preset"] is None
    assert doc["plan"]["targets"]["if_available"] == []
    for section in ("functional_disconnection", "tract_disconnection", "cognitive_profile"):
        assert doc[section] == {"status": "not_requested"}
        assert doc["sections"][section] == "not_requested"
    reasons = {s["slot"]: s["reason"] for s in doc["provenance"]["slots"] if s["reason"]}
    assert reasons["tract-disconnection"] == "not_requested"


def test_report_json_golden_external_tool_and_declared_origin_en(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference, ports=NO_COGNET).with_run_record(EXTERNAL_RUN)
    assert staged.run() == EXIT_OK
    data = staged.json_bytes()
    _check_golden_bytes("report_external_en.json", data)
    doc = json.loads(data)
    SCHEMA.validate(doc)
    assert doc["plan"] == {"mode": "full", "preset": None, "targets": {"required": [], "if_available": []}}
    assert [t["slot"] for t in doc["external_tools"]] == ["functional-disconnection"]
    tool = doc["external_tools"][0]
    assert tool["upstream"]["name"] == "example-fc-tool" and tool["upstream"]["licence"] == "Apache-2.0"
    assert tool["upstream"]["citation"].startswith("Example Authors (2025)")
    # no validation level is invented: the record carries none, so the report has no such field
    assert "validation" not in json.dumps(doc)
    assert doc["declared_origins"] == [
        {
            "name": "lesion_mni",
            "type": "lesion_mask_mni_any",
            "sha256": "7039a1152e72b0cc78c6ca4048eeb4b0297714bca3b3a686d8bd27da68103356",
            "origin": {"kind": "external-tool", "tool": "example-segmenter", "version": "2.1.0"},
        }
    ]
    assert doc["notes"] == ["external_tool_used", "input_origin_declared_by_user"]
    functional = next(
        s for s in doc["provenance"]["slots"] if s["slot"] == "functional-disconnection"
    )
    assert functional["implementation"]["upstream"] == tool["upstream"]


def test_manual_origin_is_reported(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference, ports=NO_COGNET).with_run_record(EXTERNAL_RUN)
    path = staged.in_dir / "run_provenance.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["inputs"][0]["origin"] = {"kind": "manual"}
    path.write_text(json.dumps(record), encoding="utf-8")
    assert staged.run() == EXIT_OK
    doc = json.loads(staged.json_bytes())
    SCHEMA.validate(doc)
    assert doc["declared_origins"][0]["origin"] == {"kind": "manual"}


def test_run_record_that_violates_its_schema_is_rejected(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference, ports=NO_COGNET).with_run_record(EXTERNAL_RUN)
    path = staged.in_dir / "run_provenance.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["inputs"][0]["origin"] = {"kind": "external-tool", "tool": "free text with spaces", "version": "1"}
    path.write_text(json.dumps(record), encoding="utf-8")
    assert staged.run() == EXIT_REJECTED
    assert staged.error()["port"] == "run_provenance"


def test_manifest_declares_contract_1_2_and_image_copies_the_schemas():
    manifest = (STEP_DIR / "step.yaml").read_text(encoding="utf-8")
    assert 'contract_version: "1.2"' in manifest
    dockerfile = (STEP_DIR / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY contracts/schemas /opt/step/contracts/schemas" in dockerfile
    schema = json.loads(
        (REPO / "contracts" / "schemas" / "v1" / "provenance.schema.json").read_text(encoding="utf-8")
    )
    assert "plan" in schema["$defs"]["run_provenance"]["properties"]


def test_report_json_golden_minimal_es(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference, ports=MINIMAL, language="es")
    assert staged.run() == EXIT_OK
    data = staged.json_bytes()
    _check_golden_bytes("report_min_es.json", data)
    doc = json.loads(data)
    SCHEMA.validate(doc)
    assert doc["language"] == "es"
    for section in ("functional_disconnection", "tract_disconnection", "cognitive_profile"):
        assert doc[section] == {"status": "absent"}
    absent = [i["port"] for i in doc["inputs"] if not i["present"]]
    assert absent == ["cognet_profile", "ct_mni", "fdisconnection_map", "tract_disconnection"]


def test_report_json_is_language_independent_except_language(tmp_path, reference, no_pdf):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = Staged(tmp_path / "a", reference)
    b = Staged(tmp_path / "b", reference, language="es")
    assert a.run() == EXIT_OK and b.run() == EXIT_OK
    da, db = json.loads(a.json_bytes()), json.loads(b.json_bytes())
    assert da.pop("language") == "en" and db.pop("language") == "es"
    assert da == db


def test_report_json_is_deterministic(tmp_path, reference, no_pdf):
    runs = []
    for name in ("first", "second"):
        (tmp_path / name).mkdir()
        staged = Staged(tmp_path / name, reference)
        assert staged.run() == EXIT_OK
        runs.append(staged.json_bytes())
    assert runs[0] == runs[1]


def test_input_digests_are_those_of_the_staged_files(tmp_path, reference, no_pdf):
    import hashlib

    staged = Staged(tmp_path, reference)
    assert staged.run() == EXIT_OK
    doc = json.loads(staged.json_bytes())
    for rec in doc["inputs"]:
        if rec["present"]:
            path = next(staged.in_dir.glob(rec["port"] + ".*"))
            assert rec["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
            assert rec["size_bytes"] == path.stat().st_size


def test_report_json_content(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference)
    assert staged.run() == EXIT_OK
    doc = json.loads(staged.json_bytes())
    tracts = doc["tract_disconnection"]["tracts"]
    assert [t["tract"] for t in tracts] == [
        "toy_cst_left", "toy_arcuate_left", "toy_cc_body", "toy_cst_right"
    ]  # fmt: skip
    assert doc["tract_disconnection"]["n_tracts_above_0_5"] == 1
    assert doc["lesion"]["hemisphere"] == "left" and doc["lesion"]["n_voxels"] == 1516
    assert doc["lesion"]["mask_agrees_with_qc"] is True
    assert "synthetic_reference_data" in doc["notes"]
    images = [s["implementation"]["image_digest"] for s in doc["provenance"]["slots"]
              if s["implementation"]]  # fmt: skip
    assert len(images) == 3 and all(re.fullmatch(r"sha256:[a-f0-9]{64}", d) for d in images)


# --- inputs that are absent or unusable ------------------------------------------------------


def test_missing_required_input_is_rejected(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference, ports=("lesion_qc", "run_provenance"))
    assert staged.run() == EXIT_REJECTED
    assert staged.error()["code"] == "input_missing"


def test_tract_value_out_of_range_is_rejected(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference)
    (staged.in_dir / "tract_disconnection.tsv").write_text(
        "tract\tdisconnection\ntoy_a\t1.5\n", encoding="utf-8"
    )
    assert staged.run() == EXIT_REJECTED
    err = staged.error()
    assert err["code"] == "input_format_invalid" and err["port"] == "tract_disconnection"


def test_duplicate_tract_is_rejected(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference)
    (staged.in_dir / "tract_disconnection.tsv").write_text(
        "tract\tdisconnection\ntoy_a\t0.5\ntoy_a\t0.25\n", encoding="utf-8"
    )
    assert staged.run() == EXIT_REJECTED
    assert staged.error()["code"] == "input_format_invalid"


def test_cognet_profile_violating_its_schema_is_rejected(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference)
    doc = make_reference.cognet_profile()
    del doc["method"]
    (staged.in_dir / "cognet_profile.json").write_text(json.dumps(doc), encoding="utf-8")
    assert staged.run() == EXIT_REJECTED
    err = staged.error()
    assert err["code"] == "input_format_invalid" and err["port"] == "cognet_profile"


EXCLUDED = {"count": 3, "fraction": 0.03, "reason": "undefined_in_disconnection_map"}


def _stage_excluded(staged: Staged, excluded) -> None:
    doc = make_reference.cognet_profile()
    if excluded is not None:
        doc["excluded_parcels"] = excluded
    # same layout as the reference file, so that equal content gives equal input digests
    (staged.in_dir / "cognet_profile.json").write_bytes(
        (json.dumps(doc, indent=2, sort_keys=True) + chr(10)).encode("utf-8")
    )


def test_report_json_golden_excluded_parcels_en(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference)
    _stage_excluded(staged, EXCLUDED)
    assert staged.run() == EXIT_OK
    data = staged.json_bytes()
    _check_golden_bytes("report_excluded_parcels_en.json", data)
    doc = json.loads(data)
    SCHEMA.validate(doc)
    assert doc["cognitive_profile"]["excluded_parcels"] == EXCLUDED
    assert "parcels_excluded_from_cognitive_profile" in doc["notes"]


@pytest.mark.parametrize("excluded", [None, {"count": 0, "fraction": 0.0, "reason": "none"}])
def test_no_excluded_parcels_gives_the_report_without_the_field(tmp_path, reference, no_pdf, excluded):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    base = Staged(tmp_path / "a", reference)
    assert base.run() == EXIT_OK
    other = Staged(tmp_path / "b", reference)
    _stage_excluded(other, excluded)
    assert other.run() == EXIT_OK
    doc = json.loads(other.json_bytes())
    # the staged profile file differs (its digest is in the inputs), nothing else does
    expected = json.loads(base.json_bytes())
    assert {k: v for k, v in doc.items() if k != "inputs"} == {
        k: v for k, v in expected.items() if k != "inputs"
    }
    assert "excluded_parcels" not in doc["cognitive_profile"]
    assert "parcels_excluded_from_cognitive_profile" not in doc["notes"]


def test_excluded_parcels_violating_the_profile_schema_is_rejected(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference)
    _stage_excluded(staged, {"count": -1, "fraction": 0.5, "reason": "x"})
    assert staged.run() == EXIT_REJECTED
    assert staged.error()["port"] == "cognet_profile"


def test_functional_map_with_nan_voxels_is_summarised_over_defined_voxels(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference)
    path = staged.in_dir / "fdisconnection_map.nii.gz"
    img = nib.load(str(path))
    data = np.asanyarray(img.dataobj).astype(np.float32)
    base_max = float(data.max())
    data[0:2, 0:2, 0:2] = np.nan  # zero-valued corner of the map
    nib.save(nib.Nifti1Image(data, img.affine), str(path))
    assert staged.run() == EXIT_OK
    doc = json.loads(staged.json_bytes())
    SCHEMA.validate(doc)
    fd = doc["functional_disconnection"]
    assert fd["n_undefined_voxels"] == 8 and fd["max"] == pytest.approx(base_max, abs=1e-4)


def test_functional_map_with_infinite_voxel_is_rejected(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference)
    path = staged.in_dir / "fdisconnection_map.nii.gz"
    img = nib.load(str(path))
    data = np.asanyarray(img.dataobj).astype(np.float32)
    data[0, 0, 0] = np.inf
    nib.save(nib.Nifti1Image(data, img.affine), str(path))
    assert staged.run() == EXIT_REJECTED
    assert staged.error()["port"] == "fdisconnection_map"


def test_lesion_qc_without_required_fields_is_rejected(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference)
    (staged.in_dir / "lesion_qc.json").write_text('{"mode": "mni_mask"}', encoding="utf-8")
    assert staged.run() == EXIT_REJECTED
    assert staged.error()["port"] == "lesion_qc"


def test_unknown_language_is_rejected(tmp_path, reference, no_pdf):
    staged = Staged(tmp_path, reference, language="fr")
    assert staged.run() == EXIT_REJECTED
    assert staged.error()["code"] == "invalid_parameters"


def test_schema_is_a_valid_json_schema():
    Draft202012Validator.check_schema(SCHEMA.schema)


def test_templates_contain_no_external_resources():
    for lang in render.LANGUAGES:
        text = (render.TEMPLATE_DIR / f"report_{lang}.html.j2").read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text
        assert report_pdftext.BANNERS[lang] in text


# --- PDF (text level) ---------------------------------------------------------------------


def _pdf_golden(staged: Staged, language: str, golden_name: str) -> Path:
    pdf = staged.out_dir / "report_pdf.pdf"
    if ACTUAL:
        Path(ACTUAL).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf, Path(ACTUAL) / f"{golden_name}.pdf")
        (Path(ACTUAL) / f"{golden_name}.txt").write_text(
            report_pdftext.readable(pdf, language), encoding="utf-8"
        )
    if UPDATE:
        (GOLDEN / f"{golden_name}.txt").write_text(
            report_pdftext.readable(pdf, language), encoding="utf-8"
        )
    return pdf


def _pdf_case(tmp_path, reference, ports, language, golden_name):
    staged = Staged(tmp_path, reference, ports=ports, language=language)
    if ports == NO_TRACT:
        staged.without_tract_slot()
    assert staged.run() == EXIT_OK
    pdf = staged.out_dir / "report_pdf.pdf"
    if ACTUAL:
        Path(ACTUAL).mkdir(parents=True, exist_ok=True)
        shutil.copyfile(pdf, Path(ACTUAL) / f"{golden_name}.pdf")
        (Path(ACTUAL) / f"{golden_name}.txt").write_text(
            report_pdftext.readable(pdf, language), encoding="utf-8"
        )
    if UPDATE:
        (GOLDEN / f"{golden_name}.txt").write_text(
            report_pdftext.readable(pdf, language), encoding="utf-8"
        )
    return staged, pdf


@needs_pdf
def test_pdf_full_en_golden_text_banner_and_provenance(tmp_path, reference):
    staged, pdf = _pdf_case(tmp_path, reference, tuple(FILES), "en", "report_full_en")
    assert report_pdftext.pages_without_banner(pdf, "en") == []
    pages = report_pdftext.page_texts(pdf)
    assert len(pages) >= 2
    assert report_pdftext.compare(pdf, GOLDEN / "report_full_en.txt", "en") is None

    # provenance table: every image digest of the run and every input digest is in the text
    flat = report_pdftext.squash("".join(pages))
    doc = json.loads(staged.json_bytes())
    for slot in doc["provenance"]["slots"]:
        if slot["implementation"]:
            assert slot["implementation"]["image"].replace(" ", "") in flat
            assert slot["implementation"]["image_digest"] in flat
    for rec in doc["inputs"]:
        if rec["present"]:
            assert rec["sha256"] in flat
    assert "5.1Stepsandcontainerimages" in flat


@needs_pdf
@pytest.mark.parametrize(
    "language, sentence",
    [
        (
            "en",
            "3 parcels (3.0 %) had no defined value in the disconnection map and were excluded "
            "from the cognitive profile.",
        ),
        (
            "es",
            "3 parcelas (3.0 %) no tenían un valor definido en el mapa de desconexión y se "
            "excluyeron del perfil cognitivo.",
        ),
    ],
)
def test_pdf_states_excluded_parcels_only_when_present(tmp_path, reference, language, sentence):
    for name, excluded in (("with", EXCLUDED), ("without", None)):
        (tmp_path / name).mkdir()
        staged = Staged(tmp_path / name, reference, language=language)
        _stage_excluded(staged, excluded)
        assert staged.run() == EXIT_OK
        pdf = staged.out_dir / "report_pdf.pdf"
        assert report_pdftext.pages_without_banner(pdf, language) == []
        flat = report_pdftext.squash("".join(report_pdftext.page_texts(pdf)))
        assert (report_pdftext.squash(sentence) in flat) == (excluded is not None)
        if ACTUAL:
            Path(ACTUAL).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pdf, Path(ACTUAL) / f"report_excluded_{name}_{language}.pdf")


@needs_pdf
def test_pdf_minimal_es_golden_text_and_absent_sections(tmp_path, reference):
    staged, pdf = _pdf_case(tmp_path, reference, MINIMAL, "es", "report_min_es")
    assert report_pdftext.pages_without_banner(pdf, "es") == []
    assert report_pdftext.compare(pdf, GOLDEN / "report_min_es.txt", "es") is None
    flat = report_pdftext.squash("".join(report_pdftext.page_texts(pdf)))
    assert flat.count(report_pdftext.squash("No disponible:")) == 2
    assert flat.count(report_pdftext.squash("No forma parte de este análisis:")) == 1
    assert "Procedencia" in flat


@needs_pdf
def test_pdf_minimal_en_says_sections_are_absent(tmp_path, reference):
    staged, pdf = _pdf_case(tmp_path, reference, MINIMAL, "en", "report_min_en")
    assert report_pdftext.pages_without_banner(pdf, "en") == []
    flat = report_pdftext.squash("".join(report_pdftext.page_texts(pdf)))
    assert flat.count(report_pdftext.squash("Not available:")) == 2
    assert flat.count(report_pdftext.squash("Not part of this analysis:")) == 1
    assert "Tractdisconnection" in flat


@needs_pdf
def test_pdf_without_tract_table_golden_text_and_section(tmp_path, reference):
    staged, pdf = _pdf_case(tmp_path, reference, NO_TRACT, "en", "report_notract_en")
    assert report_pdftext.pages_without_banner(pdf, "en") == []
    assert report_pdftext.compare(pdf, GOLDEN / "report_notract_en.txt", "en") is None
    flat = report_pdftext.squash("".join(report_pdftext.page_texts(pdf)))
    assert flat.count(report_pdftext.squash("Not part of this analysis:")) == 1
    assert report_pdftext.squash("Not available:") not in flat
    # the other optional sections are present, the tract table and its figure are not
    assert "Mostdisconnectedtract" not in flat and "toy_cst_left" not in flat


@needs_pdf
def test_pdf_preset_run_golden_text_and_plan(tmp_path, reference):
    staged = Staged(tmp_path, reference, ports=NO_COGNET).with_run_record(PRESET_RUN)
    assert staged.run() == EXIT_OK
    pdf = _pdf_golden(staged, "en", "report_preset_en")
    assert report_pdftext.pages_without_banner(pdf, "en") == []
    assert report_pdftext.compare(pdf, GOLDEN / "report_preset_en.txt", "en") is None
    flat = report_pdftext.squash("".join(report_pdftext.page_texts(pdf)))
    for text in (
        "Analysis plan",
        "Preset mni-mask-analysis version 1.1.0",
        "added (needed by a requested output)",
    ):
        assert report_pdftext.squash(text) in flat
    assert report_pdftext.squash("Not requested:") not in flat


@needs_pdf
def test_pdf_custom_run_golden_text_marks_slots_not_requested(tmp_path, reference):
    staged = Staged(tmp_path, reference, ports=MINIMAL, language="es").with_run_record(CUSTOM_RUN)
    assert staged.run() == EXIT_OK
    pdf = _pdf_golden(staged, "es", "report_custom_es")
    assert report_pdftext.pages_without_banner(pdf, "es") == []
    assert report_pdftext.compare(pdf, GOLDEN / "report_custom_es.txt", "es") is None
    flat = report_pdftext.squash("".join(report_pdftext.page_texts(pdf)))
    assert flat.count(report_pdftext.squash("No solicitado:")) == 3
    assert report_pdftext.squash("Plan personalizado") in flat
    assert report_pdftext.squash("no solicitado en este plan") in flat


@needs_pdf
@pytest.mark.parametrize("language", ["en", "es"])
def test_pdf_external_tool_and_declared_origin(tmp_path, reference, language):
    staged = Staged(tmp_path, reference, ports=NO_COGNET, language=language).with_run_record(
        EXTERNAL_RUN
    )
    assert staged.run() == EXIT_OK
    name = f"report_external_{language}"
    pdf = _pdf_golden(staged, language, name)
    assert report_pdftext.pages_without_banner(pdf, language) == []
    assert report_pdftext.compare(pdf, GOLDEN / f"{name}.txt", language) is None
    flat = report_pdftext.squash("".join(report_pdftext.page_texts(pdf)))
    for text in (
        "example-fc-tool 2.1.0",
        "Apache-2.0",
        "Example Authors (2025)",
        "example-segmenter 2.1.0",
    ):
        assert report_pdftext.squash(text) in flat
    declared = "declared by the user" if language == "en" else "declarado por el usuario"
    assert report_pdftext.squash(declared) in flat


@needs_pdf
def test_reference_dataset_local_run(tmp_path):
    make_reference.generate(STEP_DIR / "tests" / "reference")
    code = reftest.run_reference_test(STEP_DIR, "local", workdir=tmp_path / "work")
    assert code == 0


def test_section_slots_are_slots_of_the_stroke_lnm_pipeline():
    """`not_requested` is read from the slot names of the run record: they must be real ones."""
    import yaml

    pipeline = yaml.safe_load(
        (REPO / "pipelines" / "stroke-lnm" / "pipeline.yaml").read_text(encoding="utf-8")
    )
    names = {slot["name"] for slot in pipeline["slots"]}
    assert set(model.SECTION_SLOTS.values()) <= names
