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


def _pdf_case(tmp_path, reference, ports, language, golden_name):
    staged = Staged(tmp_path, reference, ports=ports, language=language)
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
def test_pdf_minimal_es_golden_text_and_absent_sections(tmp_path, reference):
    staged, pdf = _pdf_case(tmp_path, reference, MINIMAL, "es", "report_min_es")
    assert report_pdftext.pages_without_banner(pdf, "es") == []
    assert report_pdftext.compare(pdf, GOLDEN / "report_min_es.txt", "es") is None
    flat = report_pdftext.squash("".join(report_pdftext.page_texts(pdf)))
    assert flat.count(report_pdftext.squash("No disponible:")) == 3
    assert "Procedencia" in flat


@needs_pdf
def test_pdf_minimal_en_says_sections_are_absent(tmp_path, reference):
    staged, pdf = _pdf_case(tmp_path, reference, MINIMAL, "en", "report_min_en")
    assert report_pdftext.pages_without_banner(pdf, "en") == []
    flat = report_pdftext.squash("".join(report_pdftext.page_texts(pdf)))
    assert flat.count(report_pdftext.squash("Not available:")) == 3
    assert "Tractdisconnection" in flat


@needs_pdf
def test_reference_dataset_local_run(tmp_path):
    make_reference.generate(STEP_DIR / "tests" / "reference")
    code = reftest.run_reference_test(STEP_DIR, "local", workdir=tmp_path / "work")
    assert code == 0
