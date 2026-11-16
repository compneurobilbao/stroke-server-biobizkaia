from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.services.bids import prepare_single_subject_t1w_bids
from app.services.security import resolve_bids_subject_id
from app.services.storage import LocalStudyStorage


def test_resolve_bids_subject_id_generates_safe_default():
    study_id = uuid4()

    subject_id = resolve_bids_subject_id("", study_id)

    assert subject_id == f"sub-{study_id.hex[:8]}"


def test_resolve_bids_subject_id_rejects_invalid_value():
    with pytest.raises(HTTPException):
        resolve_bids_subject_id("patient 01", uuid4())


def test_prepare_single_subject_t1w_bids(tmp_path):
    storage = LocalStudyStorage(tmp_path / "studies")
    study_id = uuid4()
    source = tmp_path / "source.nii.gz"
    source.write_bytes(b"nifti")

    paths = prepare_single_subject_t1w_bids(storage, study_id, source, "sub-O01")

    assert paths.t1w_path.exists()
    assert paths.t1w_path.name == "sub-O01_T1w.nii.gz"
    assert (
        paths.participants_path.read_text(encoding="utf-8")
        == "participant_id\nsub-O01\n"
    )
    assert paths.dataset_description_path.exists()


# ---------------------------------------------------------------------------------
# Case store (card F0-17)
# ---------------------------------------------------------------------------------

import hashlib  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
from pathlib import Path  # noqa: E402
from uuid import UUID  # noqa: E402

import yaml  # noqa: E402

from app.services import bids  # noqa: E402
from contracts import validate as v  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FIXTURE_CASE = REPO / "fixtures" / "data" / "cases" / "case-01"
TYPES = yaml.safe_load(
    (REPO / "contracts" / "artefact-types.yaml").read_text(encoding="utf-8")
)["types"]

CASE_ID = UUID("c3a81f40-5e92-4b6d-8a17-2f9b0c6d4e85")
RUN_ID = UUID("6f1c2a9e-3b7d-4e21-9c55-0d8e4f7a1b23")
CASE = bids.CaseIdentity(
    case_id=CASE_ID, subject_label=CASE_ID.hex[:12], sex="female", age_band="65-69"
)
SUB = f"sub-{CASE_ID.hex[:12]}"
EXPORT_TEST = "test_export_tree_for_bids_validator"
IMAGE_QC = "registry.example/steps/lesion-qc@sha256:" + "a" * 64
IMAGE_NORM = "registry.example/steps/norm-stub@sha256:" + "b" * 64


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _naming(type_name: str) -> bids.BidsNaming:
    return bids.BidsNaming.from_type(TYPES[type_name])


def _file_record(port: str, type_name: str, path: Path) -> dict:
    return {
        "port": port,
        "type": type_name,
        "sha256": _sha(path),
        "size_bytes": path.stat().st_size,
    }


def _step_provenance(name: str, outputs: list[dict], inputs: list[dict]) -> dict:
    return {
        "contract_version": "1.0",
        "kind": "StepProvenance",
        "step": {"name": name, "version": "1.0.0"},
        "status": "succeeded",
        "started_at": "2026-11-13T11:00:02.398Z",
        "finished_at": "2026-11-13T11:00:31.502Z",
        "parameters": {},
        "inputs": inputs,
        "outputs": outputs,
        "bundles": [],
        "software": [{"name": "nibabel", "version": "5.3.2"}],
    }


def _execution(image: str, attempt: int = 1) -> dict:
    return {
        "executor": "docker",
        "runtime": "docker",
        "image": image,
        "attempt": attempt,
        "queued_at": "2026-11-13T11:00:00Z",
        "started_at": "2026-11-13T11:00:02Z",
        "finished_at": "2026-11-13T11:00:31Z",
        "exit_code": 0,
        "outcome": "succeeded",
        "resources": {"cpus": 1, "memory_mb": 1024, "gpus": 0, "wall_time_minutes": 10},
    }


def _slot(slot: str, step: str, image: str) -> dict:
    return {
        "slot": slot,
        "status": "selected",
        "implementation": {
            "id": step,
            "step": {"name": step, "version": "1.0.0"},
            "image": image,
            "stub": False,
            "chosen_by": "pipeline_default",
        },
        "bundles": [],
    }


def _header(inputs: list[dict]) -> dict:
    return {
        "pipeline": {"id": "stroke-lnm", "version": "0.1.0", "sha256": "c" * 64},
        "platform": {"version": "0.3.0", "artefact_types_version": "1.0.0"},
        "created_at": "2026-11-13T11:00:00Z",
        "seed": 42,
        "input_set": "ct-and-mask",
        "inputs": inputs,
        "slots": [
            _slot("lesion-intake", "lesion-qc", IMAGE_QC),
            _slot("normalisation", "norm-stub", IMAGE_NORM),
        ],
    }


def _synthetic_outputs(work: Path) -> dict[str, Path]:
    work.mkdir(parents=True, exist_ok=True)
    mni_mask = (FIXTURE_CASE / "lesion_mask_mni.nii.gz").read_bytes()
    files = {
        "lesion_qc": (work / "lesion_qc.json", b'{"volume_ml": 12.0, "flags": []}\n'),
        "qc_figure": (work / "qc_figure.png", b"\x89PNG synthetic figure bytes"),
        "mask_a": (work / "mask_a.nii.gz", mni_mask),
        "mask_b": (work / "mask_b.nii.gz", mni_mask),
    }
    for path, data in files.values():
        path.write_bytes(data)
    return {k: p for k, (p, _) in files.items()}


@pytest.fixture()
def store_root(request, tmp_path):
    # Only the export test writes to BIDS_TREE_EXPORT (set by CI); other tests, some of
    # which delete trees, always use their own temporary directory.
    export = os.environ.get("BIDS_TREE_EXPORT")
    if export and request.node.name == EXPORT_TEST:
        return Path(export)
    return tmp_path / "studies"


@pytest.fixture()
def populated(store_root, tmp_path):
    """One case with raw CT and mask, and one run with two steps."""
    storage = LocalStudyStorage(store_root)
    ct_src = FIXTURE_CASE / "ct.nii.gz"
    ct = bids.store_raw_input(
        storage,
        CASE,
        _naming("ct_native"),
        ct_src,
        expected_sha256=_sha(ct_src),
        platform_version="0.3.0",
    )
    mask = bids.store_raw_input(
        storage,
        CASE,
        _naming("lesion_mask_native"),
        FIXTURE_CASE / "lesion_mask_native.nii.gz",
    )
    inputs = [
        {
            "name": "ct",
            "type": "ct_native",
            "sha256": ct.sha256,
            "size_bytes": ct.size_bytes,
        },
        {
            "name": "lesion",
            "type": "lesion_mask_native",
            "sha256": mask.sha256,
            "size_bytes": mask.size_bytes,
        },
    ]
    bids.init_run_provenance(storage, CASE_ID, RUN_ID, _header(inputs))

    out = _synthetic_outputs(tmp_path / "out")
    prov1 = tmp_path / "prov1.json"
    prov1.write_text(
        json.dumps(
            _step_provenance(
                "lesion-qc",
                [
                    _file_record("lesion_qc", "lesion_qc", out["lesion_qc"]),
                    _file_record("qc_figure", "qc_figure", out["qc_figure"]),
                ],
                [
                    {
                        "port": "ct",
                        "type": "ct_native",
                        "sha256": ct.sha256,
                        "size_bytes": ct.size_bytes,
                    }
                ],
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    r1 = bids.store_step_result(
        storage,
        CASE,
        RUN_ID,
        slot="lesion-intake",
        step_name="lesion-qc",
        step_version="1.0.0",
        execution=_execution(IMAGE_QC),
        image=IMAGE_QC,
        provenance_path=prov1,
        outputs=[
            bids.StepOutput("lesion_qc", _naming("lesion_qc"), out["lesion_qc"]),
            bids.StepOutput("qc_figure", _naming("qc_figure"), out["qc_figure"]),
        ],
    )
    prov2 = tmp_path / "prov2.json"
    prov2.write_text(
        json.dumps(
            _step_provenance(
                "norm-stub",
                [
                    _file_record("mask_a", "lesion_mask_mni", out["mask_a"]),
                    _file_record("mask_b", "lesion_mask_mni", out["mask_b"]),
                ],
                [
                    {
                        "port": "lesion_qc",
                        "type": "lesion_qc",
                        "sha256": _sha(out["lesion_qc"]),
                        "size_bytes": out["lesion_qc"].stat().st_size,
                    }
                ],
            ),
            indent=2,
        ),
        encoding="utf-8",
    )
    r2 = bids.store_step_result(
        storage,
        CASE,
        RUN_ID,
        slot="normalisation",
        step_name="norm-stub",
        step_version="1.0.0",
        execution=_execution(IMAGE_NORM),
        image=IMAGE_NORM,
        provenance_path=prov2,
        outputs=[
            bids.StepOutput("mask_a", _naming("lesion_mask_mni"), out["mask_a"]),
            bids.StepOutput("mask_b", _naming("lesion_mask_mni"), out["mask_b"]),
        ],
    )
    bids.update_run_provenance(
        storage, CASE_ID, RUN_ID, status="succeeded", finished_at="2026-11-13T11:41:27Z"
    )
    return storage, {
        "ct": ct,
        "mask": mask,
        "r1": r1,
        "r2": r2,
        "prov1": prov1,
        "prov2": prov2,
        "out": out,
    }


@pytest.mark.skipif(
    not os.environ.get("BIDS_TREE_EXPORT"), reason="BIDS_TREE_EXPORT not set"
)
def test_export_tree_for_bids_validator(populated):
    """Produces the tree that the CI job feeds to the BIDS validator (raw and derivatives)."""
    storage, _ = populated
    assert (storage.rawdata_dir(CASE_ID) / "dataset_description.json").is_file()
    assert any(storage.derivatives_dir(CASE_ID, RUN_ID).iterdir())


def test_raw_tree_layout_and_privacy(populated):
    storage, parts = populated
    raw = storage.rawdata_dir(CASE_ID)
    assert parts["ct"].relative_path == (
        f"{CASE_ID}/rawdata/{SUB}/ses-acute/ct/{SUB}_ses-acute_ct.nii.gz"
    )
    assert (raw / SUB / "ses-acute" / "ct" / f"{SUB}_ses-acute_ct.nii.gz").is_file()
    assert parts["mask"].relative_path.endswith(
        f"{SUB}_ses-acute_label-lesion_mask.nii.gz"
    )
    description = json.loads((raw / "dataset_description.json").read_text())
    assert description["DatasetType"] == "raw"
    assert description["GeneratedBy"][0]["Name"] == "stroke-server"
    assert (raw / "participants.tsv").read_text() == (
        f"participant_id\tsex\tage_band\n{SUB}\tfemale\t65-69\n"
    )
    # Only the band is stored: no exact-age or birth column anywhere in the raw tree.
    for path in raw.rglob("*"):
        if path.is_file() and path.suffix in {".tsv", ".json"}:
            text = path.read_text(encoding="utf-8").lower()
            assert "birth" not in text
            assert "	age	" not in text and '"age"' not in text


def test_derivative_layout_generated_by_and_collisions(populated):
    storage, parts = populated
    deriv = storage.derivatives_dir(CASE_ID, RUN_ID)
    assert sorted(p.name for p in deriv.iterdir()) == [
        "lesion-qc-1.0.0",
        "norm-stub-1.0.0",
    ]
    qc = deriv / "lesion-qc-1.0.0"
    description = json.loads((qc / "dataset_description.json").read_text())
    assert description["DatasetType"] == "derivative"
    assert description["GeneratedBy"] == [
        {
            "Name": "lesion-qc",
            "Version": "1.0.0",
            "Container": {"Type": "docker", "Tag": IMAGE_QC},
        }
    ]
    assert (qc / SUB / "ses-acute" / f"{SUB}_ses-acute_label-lesion_qc.json").is_file()
    # Two ports of one type get distinct names through desc-<port>.
    anat = deriv / "norm-stub-1.0.0" / SUB / "ses-acute" / "anat"
    stem = f"{SUB}_ses-acute_space-MNI152NLin6Asym_res-2_label-lesion"
    assert sorted(p.name for p in anat.iterdir()) == [
        f"{stem}_desc-maska_mask.json",
        f"{stem}_desc-maska_mask.nii.gz",
        f"{stem}_desc-maskb_mask.json",
        f"{stem}_desc-maskb_mask.nii.gz",
    ]
    sidecar = json.loads((anat / f"{stem}_desc-maska_mask.json").read_text())
    assert "2" in sidecar["Resolution"]  # required by BIDS when res is an entity
    # Non-standard files are ignored for the validator, standard ones are not.
    assert "anat" not in (deriv / "norm-stub-1.0.0" / ".bidsignore").read_text()
    ignored = (qc / ".bidsignore").read_text().splitlines()
    assert "provenance.json" in ignored
    assert f"{SUB}/ses-acute/{SUB}_ses-acute_label-lesion_qc.json" in ignored


def test_step_provenance_kept_verbatim_and_merged(populated):
    storage, parts = populated
    stored = (
        storage.derivatives_dir(CASE_ID, RUN_ID) / "lesion-qc-1.0.0" / "provenance.json"
    )
    assert stored.read_bytes() == parts["prov1"].read_bytes()
    assert parts["r1"].provenance.sha256 == _sha(parts["prov1"])
    record = bids.load_run_provenance(storage, CASE_ID, RUN_ID)
    assert [s["slot"] for s in record["steps"]] == ["lesion-intake", "normalisation"]
    assert record["steps"][0]["provenance"] == json.loads(parts["prov1"].read_text())
    assert record["status"] == "succeeded"
    # Valid against the contract schema and the validator's own consistency rules.
    assert v.schema_errors(record, "provenance") == []
    assert v.check_provenance(record) == []


def test_retry_replaces_same_attempt_and_adds_new_one(populated):
    storage, parts = populated
    out = parts["out"]
    for attempt in (1, 2):
        bids.store_step_result(
            storage,
            CASE,
            RUN_ID,
            slot="lesion-intake",
            step_name="lesion-qc",
            step_version="1.0.0",
            execution=_execution(IMAGE_QC, attempt),
            outputs=[
                bids.StepOutput("qc_figure", _naming("qc_figure"), out["qc_figure"])
            ],
        )
    record = bids.load_run_provenance(storage, CASE_ID, RUN_ID)
    attempts = sorted(
        s["execution"]["attempt"]
        for s in record["steps"]
        if s["slot"] == "lesion-intake"
    )
    assert attempts == [1, 2]


def test_checksum_manifest_detects_tampering(populated):
    storage, _ = populated
    assert bids.verify_run_tree(storage, CASE_ID, RUN_ID) == []
    manifest = json.loads(storage.run_checksums_path(CASE_ID, RUN_ID).read_text())
    assert manifest["algorithm"] == "sha256"
    assert "provenance.json" in manifest["files"]
    victim = (
        storage.derivatives_dir(CASE_ID, RUN_ID) / "lesion-qc-1.0.0" / "provenance.json"
    )
    victim.write_text("{}")
    assert bids.verify_run_tree(storage, CASE_ID, RUN_ID) == [
        "modified: derivatives/lesion-qc-1.0.0/provenance.json"
    ]


def test_output_not_matching_provenance_is_refused(tmp_path):
    storage = LocalStudyStorage(tmp_path / "studies")
    bids.init_run_provenance(storage, CASE_ID, RUN_ID, _header([]))
    out = _synthetic_outputs(tmp_path / "out")
    prov = tmp_path / "prov.json"
    record = _step_provenance(
        "lesion-qc", [_file_record("qc_figure", "qc_figure", out["qc_figure"])], []
    )
    prov.write_text(json.dumps(record), encoding="utf-8")
    out["qc_figure"].write_bytes(b"changed after the step wrote its record")
    kwargs = {
        "slot": "lesion-intake",
        "step_name": "lesion-qc",
        "step_version": "1.0.0",
        "execution": _execution(IMAGE_QC),
        "provenance_path": prov,
    }
    with pytest.raises(bids.ChecksumMismatch):
        bids.store_step_result(
            storage,
            CASE,
            RUN_ID,
            **kwargs,
            outputs=[
                bids.StepOutput("qc_figure", _naming("qc_figure"), out["qc_figure"])
            ],
        )
    deriv = storage.derivatives_dir(CASE_ID, RUN_ID)
    assert list(deriv.rglob("*.png")) == []
    assert not list(deriv.rglob(".tmp-*"))
    assert bids.load_run_provenance(storage, CASE_ID, RUN_ID)["steps"] == []

    # An output the record does not declare is refused as well.
    with pytest.raises(bids.ChecksumMismatch):
        bids.store_step_result(
            storage,
            CASE,
            RUN_ID,
            **kwargs,
            outputs=[bids.StepOutput("extra", _naming("qc_figure"), out["qc_figure"])],
        )
    # And a record that belongs to another step.
    with pytest.raises(ValueError):
        bids.store_step_result(
            storage, CASE, RUN_ID, **{**kwargs, "step_name": "other-step"}
        )


def test_raw_input_checksum_and_identity_validation(tmp_path):
    storage = LocalStudyStorage(tmp_path / "studies")
    with pytest.raises(bids.ChecksumMismatch):
        bids.store_raw_input(
            storage,
            CASE,
            _naming("ct_native"),
            FIXTURE_CASE / "ct.nii.gz",
            expected_sha256="0" * 64,
        )
    with pytest.raises(ValueError):
        bids.CaseIdentity(case_id=CASE_ID, subject_label="Patient 01")
    with pytest.raises(ValueError):
        bids.BidsNaming(suffix="ct", extension=".nii.gz", entities={"sub": "x"})
    with pytest.raises(ValueError):
        bids.BidsNaming(suffix="../ct", extension=".nii.gz")
    with pytest.raises(ValueError):
        bids.ensure_derivative_dataset(storage, CASE, RUN_ID, "../x", "1.0.0")


def test_relative_paths_and_resolution(tmp_path):
    storage = LocalStudyStorage(tmp_path / "studies")
    path = storage.rawdata_dir(CASE_ID) / "a.txt"
    assert storage.relative_path(path) == f"{CASE_ID}/rawdata/a.txt"
    assert storage.resolve_relative(f"{CASE_ID}/rawdata/a.txt") == path.resolve()
    for bad in ["../x", "/abs", "a/../b", "a\\b", ""]:
        with pytest.raises(ValueError):
            storage.resolve_relative(bad)
    with pytest.raises(ValueError):
        storage.relative_path(tmp_path / "elsewhere")


def test_safe_deletion(populated):
    storage, _ = populated
    other_run = UUID("11111111-2222-4333-8444-555555555555")
    bids.init_run_provenance(storage, CASE_ID, other_run, _header([]))

    assert bids.delete_run_files(storage, CASE_ID, RUN_ID) is True
    assert bids.delete_run_files(storage, CASE_ID, RUN_ID) is False  # already gone
    assert storage.run_dir(CASE_ID, other_run).is_dir()
    assert storage.rawdata_dir(CASE_ID).is_dir()

    with pytest.raises(bids.RetentionBlocked):
        bids.delete_case_files(storage, CASE_ID, keep_forever=True)
    assert storage.case_dir(CASE_ID).is_dir()

    sibling = storage.root / "keep-me"
    sibling.mkdir()
    assert bids.delete_case_files(storage, CASE_ID) is True
    assert not storage.case_dir(CASE_ID).exists()
    assert sibling.is_dir()


def test_deletion_refuses_symlinks(tmp_path):
    storage = LocalStudyStorage(tmp_path / "studies")
    storage.root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("x")
    try:
        storage.case_dir(CASE_ID).symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not available")
    with pytest.raises(ValueError):
        bids.delete_case_files(storage, CASE_ID)
    assert (outside / "precious.txt").exists()


def test_legacy_study_paths_unchanged(tmp_path):
    storage = LocalStudyStorage(tmp_path / "studies")
    study_id = uuid4()
    base = tmp_path / "studies" / str(study_id)
    assert storage.study_dir(study_id) == base
    assert storage.bids_data_dir(study_id) == base / "bids_project" / "data"
    assert storage.preproc_output_dir(study_id) == base / "output" / "Preproc"
    assert storage.input_original_dir(study_id) == base / "input" / "original"
    # A back-filled case shares the study directory; the new tree sits beside the legacy one.
    assert storage.case_dir(study_id) == base
    storage.prepare(study_id)
    bids.ensure_raw_dataset(storage, bids.CaseIdentity(study_id, study_id.hex[:12]))
    assert storage.bids_data_dir(study_id).is_dir()
    assert storage.rawdata_dir(study_id).is_dir()
    storage.remove_study(study_id)
    assert not base.exists()
