"""Alembic migrations against a real PostgreSQL database.

Skipped unless TEST_POSTGRES_URL is set (the `migrations-postgres` CI job sets it). The
tests reset the `public` schema of that database, so point it at a scratch database.
Add further PostgreSQL migration tests here and mark them with `pytest.mark.postgres`.
"""

import importlib.util
import os
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.environ.get("TEST_POSTGRES_URL"),
        reason="TEST_POSTGRES_URL is not set",
    ),
]

BACKEND_DIR = Path(__file__).resolve().parents[1] / "backend"
EXPECTED_TABLES = {
    "users",
    "studies",
    "processing_jobs",
    "notifications",
    "audit_events",
}


def alembic_run(*args: str, **env_extra: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": os.environ["TEST_POSTGRES_URL"], **env_extra}
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


def alembic(*args: str, **env_extra: str) -> None:
    result = alembic_run(*args, **env_extra)
    assert (
        result.returncode == 0
    ), f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}"


@pytest.fixture()
def pg_engine():
    engine = create_engine(os.environ["TEST_POSTGRES_URL"])
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield engine
    engine.dispose()


def user_tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names()) - {"alembic_version"}


def test_upgrade_downgrade_upgrade(pg_engine):
    alembic("upgrade", "head")
    assert EXPECTED_TABLES <= user_tables(pg_engine)

    alembic("downgrade", "base")
    assert user_tables(pg_engine) == set()

    alembic("upgrade", "head")
    assert EXPECTED_TABLES <= user_tables(pg_engine)


def test_single_head(pg_engine):
    alembic("upgrade", "head")
    out = subprocess.run(
        [sys.executable, "-m", "alembic", "heads"],
        cwd=BACKEND_DIR,
        env={**os.environ, "DATABASE_URL": os.environ["TEST_POSTGRES_URL"]},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert len(out.strip().splitlines()) == 1, out


# ---------------------------------------------------------------------------------------
# Migration 0011 (case model): back-fill from a database seeded with the 0010 schema.
# ---------------------------------------------------------------------------------------

REV_BEFORE = "0010_job_cancel_requested_at"
REV_AFTER = "0011_case_model"
LEGACY_NS = uuid.uuid5(uuid.NAMESPACE_URL, "urn:stroke-server:legacy")
SYSTEM_USER_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")

NEW_TABLES = {
    "cases",
    "input_files",
    "pipeline_runs",
    "run_inputs",
    "run_slots",
    "run_slot_bundles",
    "step_runs",
    "artifacts",
    "reference_bundles",
    "reports",
    "slot_implementation_settings",
}
LEGACY_TABLES = ["users", "studies", "processing_jobs", "audit_events", "notifications", "share_links"]
BASE = datetime(2026, 3, 1, 8, 0, 0)  # legacy timestamps are naive UTC


def sid(n: int) -> uuid.UUID:
    return uuid.UUID(f"{n:08x}-0000-4000-8000-000000000000")


def jid(n: int, k: int) -> uuid.UUID:
    return uuid.UUID(f"{n:08x}-0000-4000-8000-{k:012x}")


def derived(study: uuid.UUID, suffix: str) -> uuid.UUID:
    return uuid.uuid5(LEGACY_NS, f"{study}/{suffix}")


OWNER_A = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
OWNER_B = uuid.UUID("bbbbbbbb-0000-4000-8000-000000000002")
HEX64_UPPER = "ABCDEF0123456789" * 4


class Seed:
    """Representative legacy rows written into a 0010-schema database."""

    def __init__(self, root: Path):
        self.root = root

    def p(self, study: uuid.UUID, *parts: str) -> str:
        return str(self.root / str(study) / Path(*parts))

    def apply(self, engine) -> None:
        with engine.begin() as conn:
            for uid, email in ((OWNER_A, "a@example.org"), (OWNER_B, "b@example.org")):
                conn.execute(
                    text(
                        "INSERT INTO users (id, email, full_name, role, is_active, created_at,"
                        " updated_at, notify_on_processing_completed,"
                        " notify_on_processing_failed)"
                        " VALUES (:id, :email, 'Test User', 'researcher', true, :t, :t, true, true)"
                    ),
                    {"id": uid, "email": email, "t": BASE},
                )
            # 1: uploaded, never queued, valid upper-case checksum
            self.study(conn, 1, "uploaded", OWNER_A, checksum=HEX64_UPPER, file_size=123)
            # 2: queued, one queued job
            self.study(conn, 2, "queued", OWNER_A)
            self.job(conn, 2, 1, "queued", 0)
            # 3: processing; job 1 is dead (newer job exists), job 2 is the live one
            self.study(conn, 3, "processing", OWNER_B, started=BASE + timedelta(minutes=5))
            self.job(conn, 3, 1, "processing", 0, started=BASE + timedelta(minutes=1))
            self.job(conn, 3, 2, "processing", 10, started=BASE + timedelta(minutes=11),
                     worker="worker-1")
            # 4: completed compneuro study: failed job, then completed job, pdf + zip + dir
            self.study(
                conn, 4, "completed", OWNER_A, backend="compneuro", checksum="not-a-hash",
                file_size=2048, pdf=True, zip_=True, png_dir=True,
                started=BASE + timedelta(minutes=20), finished=BASE + timedelta(minutes=40),
                warnings="low contrast", review="reviewed", bids=True,
            )
            self.job(conn, 4, 1, "failed", 0, started=BASE + timedelta(minutes=1),
                     finished=BASE + timedelta(minutes=2), exit_code=1, error="boom")
            self.job(conn, 4, 2, "completed", 15, started=BASE + timedelta(minutes=20),
                     finished=BASE + timedelta(minutes=40), exit_code=0, log=True,
                     celery="task-4-2")
            # 5: completed dummy study, pdf only, job without finished_at
            self.study(conn, 5, "completed", OWNER_B, backend="dummy", pdf=True,
                       started=BASE + timedelta(minutes=3))
            self.job(conn, 5, 1, "completed", 0, started=BASE + timedelta(minutes=3))
            # 6: failed with error message
            self.study(conn, 6, "failed", OWNER_A, error="processing failed",
                       started=BASE + timedelta(minutes=1),
                       finished=BASE + timedelta(minutes=2))
            self.job(conn, 6, 1, "failed", 0, started=BASE + timedelta(minutes=1),
                     finished=BASE + timedelta(minutes=2), exit_code=2, error="processing failed")
            # 7: canceled, soft-deleted, kept forever, cancel requested, no finished_at
            self.study(conn, 7, "canceled", OWNER_B, error="canceled by user",
                       deleted=BASE + timedelta(days=1), keep_forever=True)
            self.job(conn, 7, 1, "canceled", 0, cancel=BASE + timedelta(minutes=1),
                     error="canceled by user")
            # 8: completed study with a pdf path but no job at all
            self.study(conn, 8, "completed", OWNER_A, pdf=True)
            # dependents: events, notifications, share links
            for n, study, event in ((1, 4, "study_uploaded"), (2, 4, "study_completed")):
                conn.execute(
                    text(
                        "INSERT INTO audit_events (id, study_id, event_type, timestamp, actor)"
                        " VALUES (:id, :s, :e, :t, 'a@example.org')"
                    ),
                    {"id": jid(900, n), "s": sid(study), "e": event, "t": BASE},
                )
            conn.execute(
                text(
                    "INSERT INTO audit_events (id, study_id, event_type, timestamp, actor)"
                    " VALUES (:id, NULL, 'user_login', :t, 'a@example.org')"
                ),
                {"id": jid(900, 3), "t": BASE},
            )
            for n, study in ((1, 4), (2, None)):
                conn.execute(
                    text(
                        "INSERT INTO notifications (id, recipient_user_id, study_id, event_type,"
                        " title, message, created_at, email_status)"
                        " VALUES (:id, :u, :s, 'processing_completed', 't', 'm', :t, 'disabled')"
                    ),
                    {"id": jid(901, n), "u": OWNER_A, "s": sid(study) if study else None, "t": BASE},
                )
            conn.execute(
                text(
                    "INSERT INTO share_links (id, study_id, created_by_user_id, token_hash,"
                    " expires_at, created_at, access_count)"
                    " VALUES (:id, :s, :u, :h, :e, :t, 0)"
                ),
                {"id": jid(902, 1), "s": sid(4), "u": OWNER_A, "h": "f" * 64,
                 "e": BASE + timedelta(days=30), "t": BASE},
            )

    def study(self, conn, n, status, owner, *, backend=None, checksum=None, file_size=None,
              pdf=False, zip_=False, png_dir=False, started=None, finished=None,
              warnings=None, review="technical_only", bids=False, error=None, deleted=None,
              keep_forever=False):
        study = sid(n)
        conn.execute(
            text(
                "INSERT INTO studies (id, owner_user_id, original_filename, stored_path,"
                " output_path, pdf_path, output_zip_path, bids_subject_id, processor_backend,"
                " container_image, bids_path, preproc_output_path, rendered_png_dir,"
                " processing_warnings, clinical_review_status, status, created_at, updated_at,"
                " processing_started_at, processing_finished_at, deleted_at, keep_forever,"
                " error_message, processor_name, processor_version, file_size, checksum)"
                " VALUES (:id, :owner, :fname, :stored, :out, :pdf, :zip, :bids_id, :backend,"
                " :image, :bids, :preproc, :png, :warn, :review, CAST(:status AS studystatus),"
                " :created, :updated, :started, :finished, :deleted, :keep, :error, :pname,"
                " :pver, :size, :checksum)"
            ),
            {
                "id": study, "owner": owner, "fname": f"patient-{n}.nii.gz",
                "stored": self.p(study, "input", "original", "upload.nii.gz"),
                "out": self.p(study, "output"),
                "pdf": self.p(study, "output", "report.pdf") if pdf else None,
                "zip": self.p(study, "output", "outputs.zip") if zip_ else None,
                "bids_id": f"sub-{n}" if bids else None, "backend": backend,
                "image": "example/image:1" if backend == "compneuro" else None,
                "bids": self.p(study, "bids") if bids else None,
                "preproc": self.p(study, "preproc") if backend == "compneuro" else None,
                "png": self.p(study, "rendered") if png_dir else None,
                "warn": warnings, "review": review, "status": status,
                "created": BASE, "updated": BASE + timedelta(hours=1),
                "started": started, "finished": finished, "deleted": deleted,
                "keep": keep_forever, "error": error,
                "pname": "legacy-proc" if backend else None,
                "pver": "1.2.3" if backend else None,
                "size": file_size, "checksum": checksum,
            },
        )

    def job(self, conn, n, k, status, minute, *, started=None, finished=None, exit_code=None,
            error=None, cancel=None, worker=None, log=False, celery=None):
        study = sid(n)
        conn.execute(
            text(
                "INSERT INTO processing_jobs (id, study_id, celery_task_id, status, queued_at,"
                " started_at, finished_at, cancel_requested_at, retry_count, worker_name,"
                " exit_code, log_path, error_message)"
                " VALUES (:id, :s, :celery, :status, :queued, :started, :finished, :cancel, 0,"
                " :worker, :exit, :log, :error)"
            ),
            {
                "id": jid(n, k), "s": study, "celery": celery, "status": status,
                "queued": BASE + timedelta(minutes=minute), "started": started,
                "finished": finished, "cancel": cancel, "worker": worker, "exit": exit_code,
                "log": self.p(study, "logs", "processing.log") if log else None,
                "error": error,
            },
        )


def dump(engine, tables) -> dict[str, list[tuple]]:
    out = {}
    with engine.connect() as conn:
        for table in tables:
            rows = conn.execute(text(f"SELECT * FROM {table} ORDER BY 1, 2")).all()
            out[table] = [tuple(r) for r in rows]
    return out


def rows(engine, sql: str, **params) -> list[dict]:
    with engine.connect() as conn:
        return [dict(r) for r in conn.execute(text(sql), params).mappings()]


def one(engine, sql: str, **params) -> dict:
    result = rows(engine, sql, **params)
    assert len(result) == 1, (sql, result)
    return result[0]


def load_migration_module():
    path = BACKEND_DIR / "alembic" / "versions" / "0011_case_model.py"
    spec = importlib.util.spec_from_file_location("migration_0011", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def storage_root(tmp_path):
    root = tmp_path / "studies"
    root.mkdir()
    return root


@pytest.fixture()
def seeded(pg_engine, storage_root):
    alembic("upgrade", REV_BEFORE)
    Seed(storage_root).apply(pg_engine)
    return storage_root


def upgrade_head(root: Path) -> None:
    alembic("upgrade", "head", STORAGE_ROOT=str(root))


def test_0011_backfill_maps_legacy_rows(pg_engine, seeded):
    root = seeded
    upgrade_head(root)
    assert NEW_TABLES <= user_tables(pg_engine)

    # One case, input file, run and slot per study; ids derived from the legacy ids.
    assert one(pg_engine, "SELECT count(*) AS n FROM cases")["n"] == 8
    for table in ("input_files", "pipeline_runs", "run_inputs", "run_slots"):
        assert one(pg_engine, f"SELECT count(*) AS n FROM {table}")["n"] == 8, table
    assert one(pg_engine, "SELECT count(*) AS n FROM step_runs")["n"] == 8
    assert one(pg_engine, "SELECT count(*) AS n FROM reference_bundles")["n"] == 0

    # cases
    c4 = one(pg_engine, "SELECT * FROM cases WHERE id = :i", i=sid(4))
    assert c4["legacy_study_id"] == sid(4)
    assert c4["owner_user_id"] == OWNER_A
    assert c4["subject_label"] == sid(4).hex[:12]
    assert c4["session_label"] == "acute"
    assert c4["sex"] is None and c4["age_band"] is None
    assert c4["deployment_profile"] == "legacy"
    assert c4["created_at"] == BASE.replace(tzinfo=timezone.utc)
    assert c4["updated_at"] == (BASE + timedelta(hours=1)).replace(tzinfo=timezone.utc)
    assert c4["deleted_at"] is None and c4["keep_forever"] is False
    attrs = c4["legacy_attributes"]
    assert attrs["original_filename"] == "patient-4.nii.gz"
    assert attrs["bids_subject_id"] == "sub-4"
    assert attrs["processor_backend"] == "compneuro"
    assert attrs["processor_name"] == "legacy-proc" and attrs["processor_version"] == "1.2.3"
    assert attrs["container_image"] == "example/image:1"
    assert attrs["processing_warnings"] == "low contrast"
    assert attrs["bids_path"] == f"{sid(4)}/bids"
    assert attrs["preproc_output_path"] == f"{sid(4)}/preproc"
    assert attrs["rendered_png_dir"] == f"{sid(4)}/rendered"
    assert attrs["output_path"] == f"{sid(4)}/output"
    c7 = one(pg_engine, "SELECT * FROM cases WHERE id = :i", i=sid(7))
    assert c7["deleted_at"] == (BASE + timedelta(days=1)).replace(tzinfo=timezone.utc)
    assert c7["keep_forever"] is True

    # input files
    f1 = one(pg_engine, "SELECT * FROM input_files WHERE case_id = :i", i=sid(1))
    assert f1["id"] == derived(sid(1), "input")
    assert f1["role"] == "legacy_upload" and f1["artefact_type"] == "legacy_upload"
    assert f1["storage_path"] == f"{sid(1)}/input/original/upload.nii.gz"
    assert f1["sha256"] == HEX64_UPPER.lower() and f1["size_bytes"] == 123
    assert f1["uploaded_by_user_id"] == OWNER_A
    f4 = one(pg_engine, "SELECT * FROM input_files WHERE case_id = :i", i=sid(4))
    assert f4["sha256"] is None and f4["size_bytes"] == 2048
    ri = one(pg_engine, "SELECT * FROM run_inputs WHERE pipeline_run_id = :i", i=sid(4))
    assert ri["role"] == "legacy_upload" and ri["input_file_id"] == f4["id"]

    # runs: status mapping and timestamps
    expected = {1: "queued", 2: "queued", 3: "running", 4: "succeeded", 5: "succeeded",
                6: "failed", 7: "canceled", 8: "succeeded"}
    for n, status in expected.items():
        run = one(pg_engine, "SELECT * FROM pipeline_runs WHERE id = :i", i=sid(n))
        assert run["status"] == status, n
        assert run["case_id"] == sid(n)
        assert (run["pipeline_id"], run["pipeline_version"], run["pipeline_sha256"]) == (
            "legacy", "0.0.0", None)
        assert (run["platform_version"], run["artefact_types_version"], run["input_set"],
                run["seed"]) == ("legacy", "legacy", "legacy-single-file", 0)
        assert run["requested_implementations"] == {}
        assert run["accounting_submit_as"] == "service-account"
        assert run["accounting_billing_account"] is None
        assert run["accounting_submitted_by"] == f"user:{run['created_by_user_id']}"
        assert (run["finished_at"] is not None) == (status in ("succeeded", "failed", "canceled"))
    r3 = one(pg_engine, "SELECT * FROM pipeline_runs WHERE id = :i", i=sid(3))
    assert r3["started_at"] == (BASE + timedelta(minutes=5)).replace(tzinfo=timezone.utc)
    assert r3["resume_count"] == 1 and r3["failure_code"] is None
    r4 = one(pg_engine, "SELECT * FROM pipeline_runs WHERE id = :i", i=sid(4))
    assert r4["finished_at"] == (BASE + timedelta(minutes=40)).replace(tzinfo=timezone.utc)
    assert r4["resume_count"] == 1
    assert r4["legacy_clinical_review_status"] == "reviewed"
    r5 = one(pg_engine, "SELECT * FROM pipeline_runs WHERE id = :i", i=sid(5))
    assert r5["finished_at"] == (BASE + timedelta(hours=1)).replace(tzinfo=timezone.utc)  # updated_at
    assert r5["resume_count"] == 0
    r6 = one(pg_engine, "SELECT * FROM pipeline_runs WHERE id = :i", i=sid(6))
    assert r6["failure_code"] == "legacy_failure"
    assert r6["failure_message"] == "processing failed"
    r7 = one(pg_engine, "SELECT * FROM pipeline_runs WHERE id = :i", i=sid(7))
    assert r7["failure_code"] is None and r7["failure_message"] == "canceled by user"
    assert r7["cancel_requested_at"] == (BASE + timedelta(minutes=1)).replace(tzinfo=timezone.utc)
    assert r7["finished_at"] == (BASE + timedelta(hours=1)).replace(tzinfo=timezone.utc)

    # slots: one selected 'legacy' slot per run
    s4 = one(pg_engine, "SELECT * FROM run_slots WHERE pipeline_run_id = :i", i=sid(4))
    assert s4["id"] == derived(sid(4), "slot/legacy")
    assert (s4["slot"], s4["position"], s4["optional"], s4["status"]) == (
        "legacy", 0, False, "selected")
    assert (s4["implementation_id"], s4["step_name"], s4["step_version"], s4["stub"]) == (
        "compneuro", "legacy-compneuro", "0.0.0", False)
    assert s4["image"] is None and s4["chosen_by"] == "deployment_default"
    assert s4["effective_parameters"] == {}
    s5 = one(pg_engine, "SELECT * FROM run_slots WHERE pipeline_run_id = :i", i=sid(5))
    assert s5["implementation_id"] == "dummy" and s5["stub"] is True
    s1 = one(pg_engine, "SELECT * FROM run_slots WHERE pipeline_run_id = :i", i=sid(1))
    assert s1["implementation_id"] == "unknown" and s1["step_name"] == "legacy-unknown"

    # step runs (processing_jobs -> attempts)
    j = {(n, k): one(pg_engine, "SELECT * FROM step_runs WHERE id = :i", i=jid(n, k))
         for n, k in ((2, 1), (3, 1), (3, 2), (4, 1), (4, 2), (5, 1), (6, 1), (7, 1))}
    assert one(pg_engine, "SELECT count(*) AS n FROM step_runs WHERE legacy_job_id = id")["n"] == 8
    assert j[(2, 1)]["status"] == "queued" and j[(2, 1)]["outcome"] is None
    assert j[(2, 1)]["attempt"] == 1 and j[(2, 1)]["trigger"] == "initial"
    assert j[(2, 1)]["executor"] == "subprocess" and j[(2, 1)]["runtime"] == "none"
    assert j[(2, 1)]["run_slot_id"] == derived(sid(2), "slot/legacy")
    assert j[(2, 1)]["pipeline_run_id"] == sid(2)
    # dead job: newer job exists -> failed, finished at the next job's queue time
    dead = j[(3, 1)]
    assert (dead["status"], dead["outcome"], dead["failure_code"]) == (
        "failed", "failed", "legacy_stale_job")
    assert dead["finished_at"] == (BASE + timedelta(minutes=10)).replace(tzinfo=timezone.utc)
    live = j[(3, 2)]
    assert (live["status"], live["outcome"], live["attempt"], live["trigger"]) == (
        "running", None, 2, "user_resume")
    assert live["finished_at"] is None
    assert live["executor_state"] == {"worker_name": "worker-1"}
    assert j[(4, 1)]["status"] == "failed" and j[(4, 1)]["outcome"] == "failed"
    assert j[(4, 1)]["exit_code"] == 1 and j[(4, 1)]["failure_message"] == "boom"
    assert j[(4, 1)]["failure_code"] is None
    done = j[(4, 2)]
    assert (done["status"], done["outcome"], done["attempt"]) == ("succeeded", "succeeded", 2)
    assert done["log_path"] == f"{sid(4)}/logs/processing.log"
    assert done["orchestrator_task_id"] == "task-4-2" and done["exit_code"] == 0
    assert j[(5, 1)]["finished_at"] == (BASE + timedelta(minutes=3)).replace(tzinfo=timezone.utc)
    assert j[(7, 1)]["status"] == "canceled" and j[(7, 1)]["outcome"] == "canceled"
    assert j[(7, 1)]["cancel_requested_at"] == (BASE + timedelta(minutes=1)).replace(
        tzinfo=timezone.utc)
    assert j[(7, 1)]["finished_at"] == j[(7, 1)]["queued_at"]

    # artefacts and reports hang on the latest completed job
    arts = {a["port"]: a for a in rows(pg_engine, "SELECT * FROM artifacts WHERE case_id = :i",
                                       i=sid(4))}
    assert set(arts) == {"technical_report", "outputs_zip"}
    pdf = arts["technical_report"]
    assert pdf["id"] == derived(sid(4), "artifact/technical_report")
    assert pdf["step_run_id"] == jid(4, 2) and pdf["origin"] == "legacy"
    assert pdf["artefact_type"] == "legacy_technical_report_pdf"
    assert pdf["storage_path"] == f"{sid(4)}/output/report.pdf"
    assert pdf["sha256"] is None and pdf["size_bytes"] is None and pdf["synthetic"] is False
    assert arts["outputs_zip"]["id"] == derived(sid(4), "artifact/outputs_zip")
    assert arts["outputs_zip"]["artefact_type"] == "legacy_outputs_zip"
    rep = one(pg_engine, "SELECT * FROM reports WHERE case_id = :i", i=sid(4))
    assert rep["id"] == derived(sid(4), "report")
    assert (rep["template"], rep["language"]) == ("legacy-technical", "es")
    assert rep["pdf_artifact_id"] == pdf["id"] and rep["json_artifact_id"] is None
    assert rep["content_sha256"] is None and rep["step_run_id"] == jid(4, 2)
    a5 = one(pg_engine, "SELECT * FROM artifacts WHERE case_id = :i", i=sid(5))
    assert a5["synthetic"] is True and a5["step_run_id"] == jid(5, 1)
    # a study with a pdf path but no job gets neither artefact nor report
    assert rows(pg_engine, "SELECT * FROM artifacts WHERE case_id = :i", i=sid(8)) == []
    assert rows(pg_engine, "SELECT * FROM reports WHERE case_id = :i", i=sid(8)) == []
    assert one(pg_engine, "SELECT count(*) AS n FROM artifacts")["n"] == 3
    assert one(pg_engine, "SELECT count(*) AS n FROM reports")["n"] == 2

    # dependent tables
    events = {e["event_type"]: e for e in rows(pg_engine, "SELECT * FROM audit_events")}
    assert events["study_uploaded"]["case_id"] == sid(4)
    assert events["study_completed"]["case_id"] == sid(4)
    assert events["user_login"]["case_id"] is None
    assert all(e["pipeline_run_id"] is None for e in events.values())
    notes = rows(pg_engine, "SELECT * FROM notifications ORDER BY id")
    assert [n["case_id"] for n in notes] == [sid(4), None]
    share = one(pg_engine, "SELECT * FROM share_links")
    assert share["case_id"] == sid(4) and share["report_id"] is None

    # legacy tables are untouched
    assert one(pg_engine, "SELECT count(*) AS n FROM studies")["n"] == 8
    assert one(pg_engine, "SELECT count(*) AS n FROM processing_jobs")["n"] == 8


def test_0011_backfill_is_idempotent(pg_engine, seeded):
    upgrade_head(seeded)
    before = dump(pg_engine, sorted(NEW_TABLES))
    legacy_before = dump(pg_engine, LEGACY_TABLES)
    migration = load_migration_module()
    with pg_engine.begin() as conn:
        counts = migration.backfill(conn, seeded)
    assert counts["cases"] == 8  # rows offered, none of them inserted again
    assert dump(pg_engine, sorted(NEW_TABLES)) == before
    assert dump(pg_engine, LEGACY_TABLES) == legacy_before


def test_0011_backfill_does_not_overwrite_mirror_rows(pg_engine, seeded):
    """Rows written later by the worker (stage 1b) with the same ids are left alone."""
    upgrade_head(seeded)
    with pg_engine.begin() as conn:
        conn.execute(
            text("UPDATE pipeline_runs SET status = 'failed', finished_at = now(),"
                 " failure_code = 'step_failed' WHERE id = :i"),
            {"i": sid(2)},
        )
    migration = load_migration_module()
    with pg_engine.begin() as conn:
        migration.backfill(conn, seeded)
    assert one(pg_engine, "SELECT status FROM pipeline_runs WHERE id = :i", i=sid(2))["status"] == "failed"


def test_0011_downgrade_restores_the_0010_schema(pg_engine, seeded):
    legacy_before = dump(pg_engine, LEGACY_TABLES)
    columns_before = {
        t: [c["name"] for c in inspect(pg_engine).get_columns(t)] for t in LEGACY_TABLES
    }
    upgrade_head(seeded)
    assert one(pg_engine, "SELECT count(*) AS n FROM cases")["n"] == 8

    # downgrading is allowed: every case was back-filled from a study
    alembic("downgrade", REV_BEFORE)
    assert not (NEW_TABLES & user_tables(pg_engine))
    assert dump(pg_engine, LEGACY_TABLES) == legacy_before
    inspector = inspect(pg_engine)
    for table in LEGACY_TABLES:
        assert [c["name"] for c in inspector.get_columns(table)] == columns_before[table]
    with pg_engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == REV_BEFORE
    constraints = {c["name"] for c in inspector.get_check_constraints("users")}
    assert "ck_users_role" not in constraints

    # and the upgrade can be repeated on the downgraded database
    upgrade_head(seeded)
    assert one(pg_engine, "SELECT count(*) AS n FROM cases")["n"] == 8
    assert one(pg_engine, "SELECT count(*) AS n FROM step_runs")["n"] == 8


def test_0011_downgrade_refuses_to_lose_new_data(pg_engine, seeded):
    upgrade_head(seeded)
    new_case = uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cases (id, owner_user_id, subject_label, deployment_profile,"
                " created_at, updated_at) VALUES (:i, :u, :l, 'research-public', now(), now())"
            ),
            {"i": new_case, "u": OWNER_A, "l": new_case.hex[:12]},
        )
    refused = alembic_run("downgrade", REV_BEFORE, STORAGE_ROOT=str(seeded))
    assert refused.returncode != 0
    assert "allow_data_loss" in refused.stderr
    assert "1 cases created by new code" in refused.stderr
    assert "cases" in user_tables(pg_engine)  # nothing was dropped

    alembic("-x", "allow_data_loss=true", "downgrade", REV_BEFORE, STORAGE_ROOT=str(seeded))
    assert not (NEW_TABLES & user_tables(pg_engine))
    assert one(pg_engine, "SELECT count(*) AS n FROM studies")["n"] == 8


def test_0011_aborts_and_lists_unmappable_legacy_rows(pg_engine, storage_root, tmp_path):
    alembic("upgrade", REV_BEFORE)
    seed = Seed(storage_root)
    seed.apply(pg_engine)
    outside = tmp_path / "elsewhere" / "report.pdf"
    with pg_engine.begin() as conn:
        # a path outside the storage root
        conn.execute(
            text("UPDATE studies SET pdf_path = :p WHERE id = :i"),
            {"p": str(outside), "i": sid(5)},
        )
        # two completed jobs for one study
        seed.job(conn, 4, 3, "completed", 30, finished=BASE + timedelta(minutes=50))
    result = alembic_run("upgrade", "head", STORAGE_ROOT=str(storage_root))
    assert result.returncode != 0
    assert str(sid(5)) in result.stderr and "outside the storage root" in result.stderr
    assert str(sid(4)) in result.stderr and "more than one completed job" in result.stderr
    # PostgreSQL DDL is transactional: nothing was created, the revision did not move
    assert not (NEW_TABLES & user_tables(pg_engine))
    with pg_engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == REV_BEFORE


def test_0011_empty_database_and_relative_storage_root(pg_engine, tmp_path):
    alembic("upgrade", REV_BEFORE)
    # a relative STORAGE_ROOT (as in the CI job) works when the legacy paths use it as well
    alembic("upgrade", "head", STORAGE_ROOT="./test-data/studies")
    assert one(pg_engine, "SELECT count(*) AS n FROM cases")["n"] == 0
    alembic("downgrade", REV_BEFORE)
    assert not (NEW_TABLES & user_tables(pg_engine))


def test_0011_relative_legacy_paths_are_made_relative(pg_engine):
    alembic("upgrade", REV_BEFORE)
    study = sid(20)
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO studies (id, owner_user_id, original_filename, stored_path,"
                " clinical_review_status, status, created_at, updated_at, keep_forever)"
                " VALUES (:i, :u, 'x.nii.gz', :p, 'technical_only', 'uploaded', :t, :t, false)"
            ),
            {"i": study, "u": SYSTEM_USER_ID, "p": f"./test-data/studies/{study}/input/x.nii.gz",
             "t": BASE},
        )
    alembic("upgrade", "head", STORAGE_ROOT="./test-data/studies")
    f = one(pg_engine, "SELECT * FROM input_files WHERE case_id = :i", i=study)
    assert f["storage_path"] == f"{study}/input/x.nii.gz"
    assert one(pg_engine, "SELECT owner_user_id FROM cases WHERE id = :i", i=study)[
        "owner_user_id"] == SYSTEM_USER_ID


def test_0011_database_constraints(pg_engine, seeded):
    upgrade_head(seeded)

    def fails(sql: str, **params) -> str:
        with pytest.raises(IntegrityError) as err:
            with pg_engine.begin() as conn:
                conn.execute(text(sql), params)
        return str(err.value)

    case_cols = "id, owner_user_id, subject_label, deployment_profile, created_at, updated_at"
    case_vals = "(:i, :u, :l, 'research-public', now(), now())"
    ok = uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(
            text(f"INSERT INTO cases ({case_cols}, sex, age_band) VALUES"
                 " (:i, :u, :l, 'research-public', now(), now(), 'female', '90-104')"),
            {"i": ok, "u": OWNER_A, "l": ok.hex[:12]},
        )
    bad = uuid.uuid4()
    base = {"i": bad, "u": OWNER_A, "l": bad.hex[:12]}
    assert "ck_cases_subject_label_format" in fails(
        f"INSERT INTO cases ({case_cols}) VALUES {case_vals}", **{**base, "l": "NOT-HEX"})
    assert "uq_cases_subject_label" in fails(
        f"INSERT INTO cases ({case_cols}) VALUES {case_vals}", **{**base, "l": ok.hex[:12]})
    assert "ck_cases_sex" in fails(
        f"INSERT INTO cases ({case_cols}, sex) VALUES (:i, :u, :l, 'research-public', now(),"
        " now(), 'other')", **base)
    assert "ck_cases_age_band_format" in fails(
        f"INSERT INTO cases ({case_cols}, age_band) VALUES (:i, :u, :l, 'research-public',"
        " now(), now(), '65')", **base)
    assert "ck_cases_deployment_profile" in fails(
        f"INSERT INTO cases ({case_cols}) VALUES (:i, :u, :l, 'bogus', now(), now())", **base)

    # at most one active run per case; a finished run does not count
    run_cols = (
        "id, case_id, created_by_user_id, pipeline_id, pipeline_version, pipeline_sha256,"
        " platform_version, artefact_types_version, input_set, seed, status,"
        " requested_implementations, accounting_submitted_by, accounting_submit_as,"
        " created_at, updated_at, finished_at"
    )

    def run_sql(status: str, finished: str, extra_cols="", extra_vals="") -> str:
        return (
            f"INSERT INTO pipeline_runs ({run_cols}{extra_cols}) VALUES (:r, :c, :u,"
            " 'ct-pipeline', '1.0.0', :sha, '1.0', '1.0', 'ct-only', 7, '" + status + "',"
            " '{}'::jsonb, 'user:x', 'service-account', now(), now(), " + finished + extra_vals + ")"
        )

    sha = "a" * 64
    with pg_engine.begin() as conn:
        conn.execute(text(run_sql("succeeded", "now()")), {"r": uuid.uuid4(), "c": ok, "u": OWNER_A, "sha": sha})
        conn.execute(text(run_sql("running", "NULL")), {"r": uuid.uuid4(), "c": ok, "u": OWNER_A, "sha": sha})
    msg = fails(run_sql("queued", "NULL"), r=uuid.uuid4(), c=ok, u=OWNER_A, sha=sha)
    assert "uq_pipeline_runs_active_case" in msg
    assert "ck_pipeline_runs_finished_at" in fails(
        run_sql("succeeded", "NULL"), r=uuid.uuid4(), c=ok, u=OWNER_A, sha=sha)
    assert "ck_pipeline_runs_status" in fails(
        run_sql("done", "NULL"), r=uuid.uuid4(), c=sid(1), u=OWNER_A, sha=sha)
    assert "ck_pipeline_runs_pipeline_sha256" in fails(
        run_sql("succeeded", "now()"), r=uuid.uuid4(), c=sid(1), u=OWNER_A, sha=None)
    assert "ck_pipeline_runs_pipeline_version_format" in fails(
        run_sql("succeeded", "now()").replace("'1.0.0'", "'v1'"),
        r=uuid.uuid4(), c=sid(1), u=OWNER_A, sha=sha)
    assert "ck_pipeline_runs_seed" in fails(
        run_sql("succeeded", "now()").replace(", 7,", ", -1,"),
        r=uuid.uuid4(), c=sid(1), u=OWNER_A, sha=sha)

    # step runs: one success per slot, outcome agrees with status
    slot = derived(sid(2), "slot/legacy")
    step_sql = (
        "INSERT INTO step_runs (id, pipeline_run_id, run_slot_id, attempt, trigger, status,"
        " outcome, executor, runtime, queued_at, finished_at) VALUES (:i, :r, :s, :a,"
        " 'user_resume', :st, :o, 'subprocess', 'none', now(), :f)"
    )
    assert "ck_step_runs_status_outcome" in fails(
        step_sql, i=uuid.uuid4(), r=sid(2), s=slot, a=5, st="failed", o="succeeded", f=BASE)
    assert "ck_step_runs_status_outcome" in fails(
        step_sql, i=uuid.uuid4(), r=sid(2), s=slot, a=5, st="failed", o=None, f=BASE)
    assert "uq_step_runs_one_active" in fails(
        step_sql, i=uuid.uuid4(), r=sid(2), s=slot, a=5, st="running", o=None, f=None)
    dup_slot = derived(sid(4), "slot/legacy")
    assert "uq_step_runs_one_success" in fails(
        step_sql, i=uuid.uuid4(), r=sid(4), s=dup_slot, a=5, st="succeeded", o="succeeded", f=BASE)
    assert "uq_step_runs_slot_attempt" in fails(
        step_sql, i=uuid.uuid4(), r=sid(4), s=dup_slot, a=2, st="failed", o="failed", f=BASE)
    msg = fails(
        step_sql.replace("'subprocess'", "'docker'"),
        i=uuid.uuid4(), r=sid(4), s=dup_slot, a=6, st="failed", o="failed", f=BASE)
    assert "ck_step_runs_image" in msg or "ck_step_runs_granted_resources" in msg

    # run slots: a selected slot needs its implementation; others need a reason
    slot_sql = (
        "INSERT INTO run_slots (id, pipeline_run_id, slot, position, optional, status, reason)"
        " VALUES (:i, :r, :s, :p, false, :st, :re)"
    )
    assert "ck_run_slots_selected" in fails(
        slot_sql, i=uuid.uuid4(), r=sid(2), s="norm", p=1, st="selected", re=None)
    assert "ck_run_slots_selected" in fails(
        slot_sql, i=uuid.uuid4(), r=sid(2), s="norm", p=1, st="skipped", re=None)
    with pg_engine.begin() as conn:
        conn.execute(text(slot_sql), dict(i=uuid.uuid4(), r=sid(2), s="norm", p=1, st="skipped",
                                          re="condition_false"))

    # roles
    user_sql = (
        "INSERT INTO users (id, email, full_name, role, is_active, created_at, updated_at,"
        " notify_on_processing_completed, notify_on_processing_failed)"
        " VALUES (:i, :e, 'T', :r, true, now(), now(), true, true)"
    )
    for role in ("reviewer", "coordinator", "service"):
        with pg_engine.begin() as conn:
            conn.execute(text(user_sql), dict(i=uuid.uuid4(), e=f"{role}@example.org", r=role))
    assert "ck_users_role" in fails(user_sql, i=uuid.uuid4(), e="x@example.org", r="superuser")

    # settings: a default must be enabled and unique per slot
    set_sql = (
        "INSERT INTO slot_implementation_settings (id, pipeline_id, slot, implementation_id,"
        " enabled, is_default, updated_at) VALUES (:i, 'ct-pipeline', 'norm', :impl, :en, :d, now())"
    )
    assert "ck_slot_impl_settings_default_enabled" in fails(
        set_sql, i=uuid.uuid4(), impl="a", en=False, d=True)
    with pg_engine.begin() as conn:
        conn.execute(text(set_sql), dict(i=uuid.uuid4(), impl="a", en=True, d=True))
    assert "uq_slot_impl_settings_default" in fails(
        set_sql, i=uuid.uuid4(), impl="b", en=True, d=True)

    # legacy tables keep accepting rows written by the legacy code (no new column needed)
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO audit_events (id, study_id, event_type, timestamp, actor)"
                 " VALUES (:i, :s, 'x', now(), 'a')"),
            {"i": uuid.uuid4(), "s": sid(1)},
        )
        conn.execute(
            text("INSERT INTO share_links (id, study_id, created_by_user_id, token_hash,"
                 " expires_at, created_at, access_count)"
                 " VALUES (:i, :s, :u, :h, now(), now(), 0)"),
            {"i": uuid.uuid4(), "s": sid(1), "u": OWNER_A, "h": "e" * 64},
        )


def test_0011_schema_matches_the_orm_models(pg_engine):
    """The migration and the SQLAlchemy models must describe the same tables."""
    from app.db.base import Base
    import app.models  # noqa: F401  (registers the tables)

    alembic("upgrade", "head")
    inspector = inspect(pg_engine)
    tables = (NEW_TABLES | {"users", "audit_events", "notifications", "share_links"})
    for name in sorted(tables):
        table = Base.metadata.tables[name]
        db_cols = {c["name"]: c for c in inspector.get_columns(name)}
        assert set(db_cols) == {c.name for c in table.columns}, name
        for column in table.columns:
            assert db_cols[column.name]["nullable"] == (
                column.nullable and not column.primary_key), (name, column.name)
        db_indexes = {i["name"] for i in inspector.get_indexes(name)}
        orm_indexes = {i.name for i in table.indexes}
        assert orm_indexes <= db_indexes, (name, orm_indexes - db_indexes)
        db_checks = {c["name"] for c in inspector.get_check_constraints(name)}
        orm_checks = {
            c.name for c in table.constraints
            if c.__class__.__name__ == "CheckConstraint" and c.name
        }
        assert orm_checks <= db_checks, (name, orm_checks - db_checks)
        db_uniques = {u["name"] for u in inspector.get_unique_constraints(name)}
        orm_uniques = {
            c.name for c in table.constraints
            if c.__class__.__name__ == "UniqueConstraint" and c.name
        }
        assert orm_uniques <= db_uniques, (name, orm_uniques - db_uniques)
    # every regex format CHECK of the migration targets an existing column
    migration = load_migration_module()
    for table, column, _pattern in migration.FORMAT_CHECKS:
        assert column in Base.metadata.tables[table].columns
        names = {c["name"] for c in inspector.get_check_constraints(table)}
        assert f"ck_{table}_{column}_format" in names
