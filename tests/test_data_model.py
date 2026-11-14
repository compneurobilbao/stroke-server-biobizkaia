"""Models and schemas of the Phase 0 data model (docs/data-model.md), on SQLite.

The PostgreSQL-only parts (regex CHECKs, the back-fill, the downgrade) are tested in
tests/test_migrations_postgres.py.
"""

import uuid

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests import pgdb
from app.models import (
    Artifact,
    Case,
    InputFile,
    PipelineRun,
    ReferenceBundle,
    Report,
    RunInput,
    RunSlot,
    RunSlotBundle,
    SlotImplementationSetting,
    StepRun,
    User,
)
from app.models.case import (
    age_band_midpoint,
    check_relative_path,
    is_valid_age_band,
    valid_age_bands,
)
from app.schemas.case import CaseCreate, CaseRead, CaseUpdate
from app.schemas.bundle import SlotImplementationSettingUpdate
from app.schemas.run import PipelineRunRead, RunSlotRead, StepRunRead

SHA = "a" * 64
IMAGE = "registry.example.org/team/step@sha256:" + "b" * 64


@pytest.fixture()
def db(tmp_path):
    engine = pgdb.new_engine(tmp_path, "model.db")
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture()
def user(db):
    row = User(email="u@example.org", full_name="U", role="researcher")
    db.add(row)
    db.commit()
    return row


def make_case(db, user, **kw) -> Case:
    case = Case(owner_user_id=user.id, deployment_profile="research-public", **kw)
    db.add(case)
    db.commit()
    return case


def make_run(db, case, status="queued", **kw) -> PipelineRun:
    values = dict(
        case_id=case.id,
        created_by_user_id=case.owner_user_id,
        pipeline_id="ct-pipeline",
        pipeline_version="1.0.0",
        pipeline_sha256=SHA,
        platform_version="1.0",
        artefact_types_version="1.0",
        input_set="ct-only",
        seed=42,
        status=status,
        accounting_submitted_by=f"user:{case.owner_user_id}",
        accounting_submit_as="service-account",
    )
    values.update(kw)
    run = PipelineRun(**values)
    db.add(run)
    db.commit()
    return run


def make_slot(db, run, **kw) -> RunSlot:
    values = dict(
        pipeline_run_id=run.id,
        slot="normalise",
        position=0,
        optional=False,
        status="selected",
        implementation_id="synthetic-stub",
        step_name="normalise-stub",
        step_version="1.0.0",
        image=IMAGE,
        stub=True,
        chosen_by="pipeline_default",
        effective_parameters={"seed": 42},
    )
    values.update(kw)
    slot = RunSlot(**values)
    db.add(slot)
    db.commit()
    return slot


def test_age_bands_follow_the_owner_decision():
    bands = valid_age_bands()
    assert bands[0] == "0-4" and bands[1] == "5-9" and bands[-2] == "85-89"
    assert bands[-1] == "90-104"
    assert len(bands) == 19
    assert is_valid_age_band("65-69") and is_valid_age_band("90-104")
    for bad in ("65", "65-70", "63-67", "90-94", "95-99", "100-104", "-5-9", "", "a-b"):
        assert not is_valid_age_band(bad), bad
    assert age_band_midpoint("65-69") == 67
    assert age_band_midpoint("90-104") == 97
    with pytest.raises(ValueError):
        age_band_midpoint("90-94")


def test_case_validators_and_defaults(db, user):
    case = make_case(db, user, sex="female", age_band="75-79")
    assert case.subject_label == case.id.hex[:12]
    assert case.session_label == "acute"
    assert case.keep_forever is False and case.legacy_study_id is None
    assert case.created_at is not None and case.updated_at is not None
    with pytest.raises(ValueError):
        Case(owner_user_id=user.id, deployment_profile="research-public", age_band="64")
    with pytest.raises(ValueError):
        Case(owner_user_id=user.id, deployment_profile="research-public", sex="unknown")
    with pytest.raises(ValueError):
        Case(
            owner_user_id=user.id,
            deployment_profile="research-public",
            subject_label="Z" * 12,
        )


def test_case_constraints(db, user):
    first = make_case(db, user)
    db.add(Case(owner_user_id=user.id, deployment_profile="bogus"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(
        Case(
            id=uuid.uuid4(),
            owner_user_id=user.id,
            deployment_profile="on-premise",
            subject_label=first.subject_label,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_input_file_rules(db, user):
    case = make_case(db, user)
    ok = InputFile(
        case_id=case.id,
        role="ct",
        artefact_type="ct_native",
        storage_path="sub-abc/ses-acute/ct.nii.gz",
        sha256=SHA,
        size_bytes=10,
        uploaded_by_user_id=user.id,
    )
    db.add(ok)
    db.commit()
    # a second file for the same role is refused
    db.add(
        InputFile(
            case_id=case.id,
            role="ct",
            artefact_type="ct_native",
            storage_path="x",
            sha256=SHA,
            size_bytes=1,
            uploaded_by_user_id=user.id,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    # sha256 and size are mandatory except for legacy uploads
    db.add(
        InputFile(
            case_id=case.id,
            role="lesion_native",
            artefact_type="lesion_mask_native",
            storage_path="y",
            uploaded_by_user_id=user.id,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(
        InputFile(
            case_id=case.id,
            role="legacy_upload",
            artefact_type="legacy_upload",
            storage_path="z",
            uploaded_by_user_id=user.id,
        )
    )
    db.commit()
    with pytest.raises(ValueError):
        InputFile(role="Not Snake", artefact_type="ct_native")
    with pytest.raises(ValueError):
        InputFile(sha256="XYZ")


@pytest.mark.parametrize("bad", ["", "/abs/path", "a/../b", "a\\b"])
def test_relative_path_check(bad):
    with pytest.raises(ValueError):
        check_relative_path(bad)
    assert check_relative_path("sub-abc/ses-acute/ct.nii.gz")


def test_one_active_run_per_case(db, user):
    case = make_case(db, user)
    make_run(db, case, status="succeeded", finished_at=case.created_at)
    make_run(db, case, status="running")
    other = make_case(db, user)
    make_run(db, other, status="queued")
    with pytest.raises(IntegrityError):
        make_run(db, case, status="queued")
    db.rollback()
    with pytest.raises(IntegrityError):
        make_run(db, case, status="awaiting_review")
    db.rollback()


def test_run_constraints(db, user):
    case = make_case(db, user)
    with pytest.raises(IntegrityError):
        make_run(db, case, status="done")
    db.rollback()
    with pytest.raises(IntegrityError):  # terminal without finished_at
        make_run(db, case, status="failed")
    db.rollback()
    with pytest.raises(IntegrityError):  # seed out of range
        make_run(db, case, status="queued", seed=2**32)
    db.rollback()
    with pytest.raises(ValueError):
        PipelineRun(pipeline_version="v1")
    run = make_run(db, case, status="queued")
    assert run.requested_implementations == {} and run.resume_count == 0


def test_run_slot_constraints(db, user):
    case = make_case(db, user)
    run = make_run(db, case)
    slot = make_slot(db, run)
    # slot names and positions are unique per run
    with pytest.raises(IntegrityError):
        make_slot(db, run, position=1)
    db.rollback()
    with pytest.raises(IntegrityError):
        make_slot(db, run, slot="other")
    db.rollback()
    # a skipped slot has a reason and no implementation
    make_slot(
        db,
        run,
        slot="optional-qc",
        position=1,
        optional=True,
        status="skipped",
        reason="condition_false",
        implementation_id=None,
        step_name=None,
        step_version=None,
        image=None,
        stub=None,
        chosen_by=None,
        effective_parameters=None,
    )
    with pytest.raises(IntegrityError):
        make_slot(db, run, slot="bad-skip", position=2, status="skipped")
    db.rollback()
    with pytest.raises(
        IntegrityError
    ):  # selected without an image (not the legacy slot)
        make_slot(db, run, slot="no-image", position=3, image=None)
    db.rollback()
    legacy = make_slot(db, run, slot="legacy", position=4, image=None)
    assert legacy.image is None
    with pytest.raises(ValueError):
        RunSlot(image="example/step:latest")
    assert slot.effective_parameters == {"seed": 42}


def test_step_run_constraints_and_relations(db, user):
    case = make_case(db, user)
    run = make_run(db, case)
    slot = make_slot(db, run)

    def attempt(n, status, outcome=None, **kw):
        values = dict(
            pipeline_run_id=run.id,
            run_slot_id=slot.id,
            attempt=n,
            trigger="initial" if n == 1 else "auto_retry",
            status=status,
            outcome=outcome,
            executor="subprocess",
            runtime="none",
            finished_at=run.created_at
            if status in ("succeeded", "failed", "canceled")
            else None,
        )
        values.update(kw)
        row = StepRun(**values)
        db.add(row)
        db.commit()
        return row

    first = attempt(1, "failed", "failed")
    assert first.queued_at is not None and first.log_truncated is False
    attempt(2, "running")
    # one live attempt per slot, unique attempt numbers, one success per slot
    with pytest.raises(IntegrityError):
        attempt(3, "queued")
    db.rollback()
    with pytest.raises(IntegrityError):
        attempt(2, "failed", "failed")
    db.rollback()
    # status and outcome must agree
    for status, outcome in (
        ("running", "failed"),
        ("succeeded", "failed"),
        ("failed", None),
        ("canceled", "failed"),
        ("failed", "succeeded"),
    ):
        with pytest.raises(IntegrityError):
            attempt(9, status, outcome)
        db.rollback()
    with pytest.raises(IntegrityError):  # rejected needs the step error
        attempt(9, "failed", "rejected")
    db.rollback()
    attempt(9, "failed", "rejected", step_error={"code": "input_rejected"})
    with pytest.raises(
        IntegrityError
    ):  # docker attempts need image and granted resources
        attempt(10, "failed", "failed", executor="docker", runtime="docker")
    db.rollback()
    attempt(
        11,
        "failed",
        "failed",
        executor="docker",
        runtime="docker",
        image=IMAGE,
        granted_cpus=2,
        granted_memory_mb=1024,
        granted_gpus=0,
        granted_wall_time_minutes=30,
    )
    # success: the second one for the slot is refused (the live attempt 2 must end first)
    db.query(StepRun).filter_by(attempt=2).update(
        {"status": "succeeded", "outcome": "succeeded", "finished_at": run.created_at}
    )
    db.commit()
    with pytest.raises(IntegrityError):
        attempt(12, "succeeded", "succeeded")
    db.rollback()
    db.refresh(slot)
    assert [s.attempt for s in slot.step_runs] == [1, 2, 9, 11]
    assert len(run.slots) == 1 and len(run.steps) == 4


def test_artifacts_reports_and_bundles(db, user):
    case = make_case(db, user)
    run = make_run(db, case)
    slot = make_slot(db, run)
    step = StepRun(
        pipeline_run_id=run.id,
        run_slot_id=slot.id,
        attempt=1,
        trigger="initial",
        status="queued",
        executor="subprocess",
        runtime="none",
    )
    db.add(step)
    db.commit()

    def artifact(port, **kw):
        values = dict(
            case_id=case.id,
            pipeline_run_id=run.id,
            step_run_id=step.id,
            origin="step",
            port=port,
            artefact_type="report_pdf",
            storage_path=f"derivatives/{port}.pdf",
            sha256=SHA,
            size_bytes=5,
            synthetic=True,
        )
        values.update(kw)
        row = Artifact(**values)
        db.add(row)
        db.commit()
        return row

    pdf = artifact("report")
    js = artifact("report_json", artefact_type="report_json")
    with pytest.raises(IntegrityError):  # one artefact per step and port
        artifact("report")
    db.rollback()
    with pytest.raises(IntegrityError):  # hash is mandatory outside legacy rows
        artifact("other", sha256=None)
    db.rollback()
    with pytest.raises(IntegrityError):  # step artefacts need a step run
        artifact("other", step_run_id=None)
    db.rollback()
    artifact(
        "legacy-port".replace("-", "_"), origin="legacy", sha256=None, size_bytes=None
    )
    # a review artefact may supersede one and has no step run
    edited = artifact(
        "report", origin="review", step_run_id=None, supersedes_artifact_id=pdf.id
    )
    assert edited.supersedes_artifact_id == pdf.id

    report = Report(
        case_id=case.id,
        pipeline_run_id=run.id,
        step_run_id=step.id,
        pdf_artifact_id=pdf.id,
        json_artifact_id=js.id,
        template="research",
        language="en",
        content_sha256=SHA,
    )
    db.add(report)
    db.commit()
    db.add(
        Report(
            case_id=case.id,
            pipeline_run_id=run.id,
            step_run_id=step.id,
            pdf_artifact_id=pdf.id,
            json_artifact_id=js.id,
            template="research",
            language="en",
            content_sha256=SHA,
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    db.add(
        Report(
            case_id=case.id,
            pipeline_run_id=run.id,
            step_run_id=step.id,
            pdf_artifact_id=pdf.id,
            template="research",
            language="es",
        )
    )
    with pytest.raises(
        IntegrityError
    ):  # JSON artefact and hash required outside legacy
        db.commit()
    db.rollback()

    bundle = ReferenceBundle(
        bundle_id="ventricle-templates",
        version="1.0.0",
        bundle_type="template-set",
        title="Templates",
        content_digest="sha256:" + SHA,
        manifest_sha256=SHA,
        synthetic=True,
        licence_spdx="CC0-1.0",
        redistributable=True,
        storage_path="ventricle-templates/1.0.0",
    )
    db.add(bundle)
    db.commit()
    assert bundle.metadata_ == {} and bundle.status == "active"
    assert bundle.verification_status == "verified"
    db.add(
        ReferenceBundle(
            bundle_id="ventricle-templates",
            version="1.0.0",
            bundle_type="template-set",
            title="Dup",
            content_digest="sha256:" + SHA,
            manifest_sha256=SHA,
            synthetic=True,
            licence_spdx="CC0-1.0",
            redistributable=True,
            storage_path="x",
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
    with pytest.raises(ValueError):
        ReferenceBundle(content_digest="md5:abc")
    db.add(
        RunSlotBundle(
            run_slot_id=slot.id,
            requirement_name="templates",
            reference_bundle_id=bundle.id,
        )
    )
    db.commit()
    assert slot.bundles[0].reference_bundle.bundle_id == "ventricle-templates"
    db.add(RunInput(pipeline_run_id=run.id, role="ct", input_file_id=uuid.uuid4()))
    db.commit()
    assert run.inputs[0].role == "ct"


def test_slot_implementation_settings(db):
    def setting(impl, enabled, default):
        row = SlotImplementationSetting(
            pipeline_id="ct-pipeline",
            slot="normalise",
            implementation_id=impl,
            enabled=enabled,
            is_default=default,
        )
        db.add(row)
        db.commit()

    setting("a", True, True)
    setting("b", True, False)
    with pytest.raises(IntegrityError):  # second default for the slot
        setting("c", True, True)
    db.rollback()
    with pytest.raises(IntegrityError):  # a default must be enabled
        setting("d", False, True)
    db.rollback()
    with pytest.raises(IntegrityError):  # one row per implementation
        setting("a", True, False)
    db.rollback()


def test_user_role_check(db):
    for role in ("admin", "researcher", "reviewer", "coordinator", "service"):
        db.add(User(email=f"{role}@example.org", full_name=role, role=role))
    db.commit()
    db.add(User(email="x@example.org", full_name="x", role="superuser"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_case_schemas():
    assert CaseCreate(sex="male", age_band="90-104").age_band == "90-104"
    assert CaseCreate().sex is None
    for bad in (
        {"age_band": "67"},
        {"age_band": "90-94"},
        {"sex": "x"},
        {"age": 67},
        {"subject_label": "abcdef012345"},
    ):
        with pytest.raises(ValidationError):
            CaseCreate(**bad)
    with pytest.raises(ValidationError):
        CaseUpdate(age_band="100-104")
    with pytest.raises(ValidationError):
        SlotImplementationSettingUpdate(enabled=False, is_default=True)
    assert SlotImplementationSettingUpdate(enabled=True, is_default=True).is_default


def test_read_schemas_from_orm(db, user):
    case = make_case(db, user, sex="male", age_band="85-89")
    read = CaseRead.model_validate(case)
    assert read.subject_label == case.id.hex[:12] and read.age_band == "85-89"
    run = make_run(db, case)
    slot = make_slot(db, run)
    assert PipelineRunRead.model_validate(run).seed == 42
    assert RunSlotRead.model_validate(slot).implementation_id == "synthetic-stub"
    step = StepRun(
        pipeline_run_id=run.id,
        run_slot_id=slot.id,
        attempt=1,
        trigger="initial",
        status="queued",
        executor="subprocess",
        runtime="none",
    )
    db.add(step)
    db.commit()
    assert StepRunRead.model_validate(step).status == "queued"


def test_0011_relative_rejects_paths_escaping_the_storage_root(tmp_path):
    import importlib.util
    from pathlib import Path

    import pytest

    migration = (
        Path(__file__).resolve().parents[1]
        / "backend/alembic/versions/0011_case_model.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0011", migration)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    root = tmp_path / "studies"
    assert module._relative(str(root / "abc" / "report.pdf"), root) == "abc/report.pdf"
    assert (
        module._relative(str(root / "abc" / ".." / "def" / "x.zip"), root)
        == "def/x.zip"
    )
    with pytest.raises(ValueError):
        module._relative(str(root / "abc" / ".." / ".." / "other" / "x.pdf"), root)
    with pytest.raises(ValueError):
        module._relative("/elsewhere/x.pdf", root)
