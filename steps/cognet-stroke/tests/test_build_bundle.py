"""Tests of bundle/build_bundle.py on synthetic "workflow outputs" (nothing real, nothing committed).

The concept z maps, the null caches and the parcellation are made here in the shapes the author's
workflow writes them; the build runs with the numpy reduction (neuromaps is not installed in CI),
which is the workflow's reduction when the maps are on the parcellation's grid.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
import yaml

STEP_DIR = Path(__file__).resolve().parents[1]
REPO = STEP_DIR.parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


builder = _load("cognet_stroke_build_bundle", STEP_DIR / "bundle" / "build_bundle.py")
make_reference = sys.modules.get("cognet_stroke_make_reference") or _load(
    "cognet_stroke_make_reference", STEP_DIR / "tests" / "make_reference.py"
)

from stroke_step_sdk import contracts  # noqa: E402

CONCEPTS = [("motor", "sensorimotor"), ("attention", "cognition"), ("emotion", "social_affective")]
N_NULLS = 20


@pytest.fixture
def workflow(tmp_path) -> dict:
    """A parcellation named like the author's and the corrected_fdr directory of the workflow."""
    shape, affine = make_reference.working_grid(REPO / "contracts")
    labels = make_reference.parcellation(shape).astype(np.float32)  # float labels, as firsT_2566
    parc = tmp_path / "firsT_test.nii.gz"
    nib.save(nib.Nifti1Image(labels, affine), str(parc))
    ids = np.unique(labels[labels > 0]).astype(int)
    maps = tmp_path / "corrected_fdr"
    maps.mkdir()
    rng = np.random.default_rng(0)
    expected = {}
    for k, (concept, _domain) in enumerate(CONCEPTS):
        inside = ids[k::5]  # parcels whose mean z exceeds 2
        z = np.where(np.isin(labels, inside), 3.0, 0.5).astype(np.float32)
        nib.save(nib.Nifti1Image(z, affine), str(maps / builder.MAP_NAME.format(concept=concept)))
        nulls = np.where(rng.random((N_NULLS, ids.size)) > 0.8, 1, -1)  # int64, as np.where gives
        np.save(maps / builder.NULL_NAME.format(concept=concept, parcellation="firsT_test"), nulls)
        expected[concept] = (np.where(np.isin(ids, inside), 1, -1), nulls)
    table = tmp_path / "concepts.tsv"
    table.write_text("concept\tdomain\n" + "".join(f"{c}\t{d}\n" for c, d in CONCEPTS))
    return {"parc": parc, "maps": maps, "table": table, "ids": ids, "expected": expected}


def build(workflow, out: Path, *extra: str) -> int:
    return builder.main(
        [
            "--parcellation", str(workflow["parc"]),
            "--maps-dir", str(workflow["maps"]),
            "--concepts", str(workflow["table"]),
            "--version", "1.1.0",
            "--out", str(out),
            "--allow-numpy-parcellation",
            *extra,
        ]
    )  # fmt: skip


def test_bundle_from_workflow_outputs(workflow, tmp_path):
    out = tmp_path / "bundle"
    assert build(workflow, out) == 0
    issues = contracts.validator().validate_files([out / "bundle.yaml"])
    assert [i.message for i in issues if i.severity == "error"] == []
    doc = yaml.safe_load((out / "bundle.yaml").read_text(encoding="utf-8"))
    assert (doc["id"], doc["version"], doc["type"]) == ("cognet-concept-maps", "1.1.0", "concept-maps")
    assert doc["licence"]["redistributable"] is False
    assert doc["metadata"]["n_parcels"] == workflow["ids"].size
    assert doc["metadata"]["n_nulls"] == N_NULLS
    assert doc["metadata"]["parcellation"] == "firsT_test"
    assert doc["metadata"]["mask_parcellation"] == "numpy-mean-same-grid"
    assert (out / "parcellation.nii.gz").read_bytes() == workflow["parc"].read_bytes()
    assert (out / "concepts.tsv").read_text().splitlines()[1:] == [f"{c}\t{d}" for c, d in CONCEPTS]
    for concept, (mask, nulls) in workflow["expected"].items():
        with np.load(out / "concepts" / f"{concept}.npz") as npz:
            assert npz["mask"].dtype == np.int8 and npz["nulls"].dtype == np.int8
            np.testing.assert_array_equal(npz["mask"], mask)
            np.testing.assert_array_equal(npz["nulls"], nulls)


def test_build_is_deterministic(workflow, tmp_path):
    digests = []
    for name in ("a", "b"):
        assert build(workflow, tmp_path / name) == 0
        digests.append(yaml.safe_load((tmp_path / name / "bundle.yaml").read_text())["content_digest"])
    assert digests[0] == digests[1]


def test_null_cache_of_another_parcellation_is_refused(workflow, tmp_path, capsys):
    path = workflow["maps"] / builder.NULL_NAME.format(concept="attention", parcellation="firsT_test")
    np.save(path, np.ones((N_NULLS, workflow["ids"].size + 1), dtype=int))
    assert build(workflow, tmp_path / "bundle") == 1
    assert "another parcellation" in capsys.readouterr().err


def test_missing_outputs_and_non_empty_destination_are_refused(workflow, tmp_path, capsys):
    os.remove(workflow["maps"] / builder.MAP_NAME.format(concept="emotion"))
    assert build(workflow, tmp_path / "bundle") == 1
    assert "missing workflow outputs" in capsys.readouterr().err
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    assert build(workflow, busy) == 1
    assert "not empty" in capsys.readouterr().err


def test_without_neuromaps_the_numpy_reduction_must_be_allowed(workflow, tmp_path, capsys):
    if importlib.util.find_spec("neuromaps") is not None:
        pytest.skip("neuromaps is installed here")
    args = [
        "--parcellation", str(workflow["parc"]), "--maps-dir", str(workflow["maps"]),
        "--concepts", str(workflow["table"]), "--version", "1.1.0", "--out", str(tmp_path / "b"),
    ]  # fmt: skip
    assert builder.main(args) == 1
    assert "neuromaps is not importable" in capsys.readouterr().err


def test_default_concept_table_is_the_authors_22_concepts():
    rows = builder.read_concept_table(builder.HERE / "concepts.tsv")
    assert len(rows) == 22 and len({c for c, _ in rows}) == 22
    assert {d for _, d in rows} == {"sensorimotor", "perception", "cognition", "social_affective"}
