"""Alembic migrations against a real PostgreSQL database.

Skipped unless TEST_POSTGRES_URL is set (the `migrations-postgres` CI job sets it). The
tests reset the `public` schema of that database, so point it at a scratch database.
Add further PostgreSQL migration tests here and mark them with `pytest.mark.postgres`.
"""

import importlib.util
import json
import os
import re
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
LEGACY_TABLES = [
    "users",
    "studies",
    "processing_jobs",
    "audit_events",
    "notifications",
    "share_links",
]
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
            self.study(
                conn, 1, "uploaded", OWNER_A, checksum=HEX64_UPPER, file_size=123
            )
            # 2: queued, one queued job
            self.study(conn, 2, "queued", OWNER_A)
            self.job(conn, 2, 1, "queued", 0)
            # 3: processing; job 1 is dead (newer job exists), job 2 is the live one
            self.study(
                conn, 3, "processing", OWNER_B, started=BASE + timedelta(minutes=5)
            )
            self.job(conn, 3, 1, "processing", 0, started=BASE + timedelta(minutes=1))
            self.job(
                conn,
                3,
                2,
                "processing",
                10,
                started=BASE + timedelta(minutes=11),
                worker="worker-1",
            )
            # 4: completed compneuro study: failed job, then completed job, pdf + zip + dir
            self.study(
                conn,
                4,
                "completed",
                OWNER_A,
                backend="compneuro",
                checksum="not-a-hash",
                file_size=2048,
                pdf=True,
                zip_=True,
                png_dir=True,
                started=BASE + timedelta(minutes=20),
                finished=BASE + timedelta(minutes=40),
                warnings="low contrast",
                review="reviewed",
                bids=True,
            )
            self.job(
                conn,
                4,
                1,
                "failed",
                0,
                started=BASE + timedelta(minutes=1),
                finished=BASE + timedelta(minutes=2),
                exit_code=1,
                error="boom",
            )
            self.job(
                conn,
                4,
                2,
                "completed",
                15,
                started=BASE + timedelta(minutes=20),
                finished=BASE + timedelta(minutes=40),
                exit_code=0,
                log=True,
                celery="task-4-2",
            )
            # 5: completed dummy study, pdf only, job without finished_at
            self.study(
                conn,
                5,
                "completed",
                OWNER_B,
                backend="dummy",
                pdf=True,
                started=BASE + timedelta(minutes=3),
            )
            self.job(conn, 5, 1, "completed", 0, started=BASE + timedelta(minutes=3))
            # 6: failed with error message
            self.study(
                conn,
                6,
                "failed",
                OWNER_A,
                error="processing failed",
                started=BASE + timedelta(minutes=1),
                finished=BASE + timedelta(minutes=2),
            )
            self.job(
                conn,
                6,
                1,
                "failed",
                0,
                started=BASE + timedelta(minutes=1),
                finished=BASE + timedelta(minutes=2),
                exit_code=2,
                error="processing failed",
            )
            # 7: canceled, soft-deleted, kept forever, cancel requested, no finished_at
            self.study(
                conn,
                7,
                "canceled",
                OWNER_B,
                error="canceled by user",
                deleted=BASE + timedelta(days=1),
                keep_forever=True,
            )
            self.job(
                conn,
                7,
                1,
                "canceled",
                0,
                cancel=BASE + timedelta(minutes=1),
                error="canceled by user",
            )
            # 8: completed study with a pdf path but no job at all
            self.study(conn, 8, "completed", OWNER_A, pdf=True)
            # dependents: events, notifications, share links
            for n, study, event in (
                (1, 4, "study_uploaded"),
                (2, 4, "study_completed"),
            ):
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
                    {
                        "id": jid(901, n),
                        "u": OWNER_A,
                        "s": sid(study) if study else None,
                        "t": BASE,
                    },
                )
            conn.execute(
                text(
                    "INSERT INTO share_links (id, study_id, created_by_user_id, token_hash,"
                    " expires_at, created_at, access_count)"
                    " VALUES (:id, :s, :u, :h, :e, :t, 0)"
                ),
                {
                    "id": jid(902, 1),
                    "s": sid(4),
                    "u": OWNER_A,
                    "h": "f" * 64,
                    "e": BASE + timedelta(days=30),
                    "t": BASE,
                },
            )

    def study(
        self,
        conn,
        n,
        status,
        owner,
        *,
        backend=None,
        checksum=None,
        file_size=None,
        pdf=False,
        zip_=False,
        png_dir=False,
        started=None,
        finished=None,
        warnings=None,
        review="technical_only",
        bids=False,
        error=None,
        deleted=None,
        keep_forever=False,
    ):
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
                "id": study,
                "owner": owner,
                "fname": f"patient-{n}.nii.gz",
                "stored": self.p(study, "input", "original", "upload.nii.gz"),
                "out": self.p(study, "output"),
                "pdf": self.p(study, "output", "report.pdf") if pdf else None,
                "zip": self.p(study, "output", "outputs.zip") if zip_ else None,
                "bids_id": f"sub-{n}" if bids else None,
                "backend": backend,
                "image": "example/image:1" if backend == "compneuro" else None,
                "bids": self.p(study, "bids") if bids else None,
                "preproc": self.p(study, "preproc") if backend == "compneuro" else None,
                "png": self.p(study, "rendered") if png_dir else None,
                "warn": warnings,
                "review": review,
                "status": status,
                "created": BASE,
                "updated": BASE + timedelta(hours=1),
                "started": started,
                "finished": finished,
                "deleted": deleted,
                "keep": keep_forever,
                "error": error,
                "pname": "legacy-proc" if backend else None,
                "pver": "1.2.3" if backend else None,
                "size": file_size,
                "checksum": checksum,
            },
        )

    def job(
        self,
        conn,
        n,
        k,
        status,
        minute,
        *,
        started=None,
        finished=None,
        exit_code=None,
        error=None,
        cancel=None,
        worker=None,
        log=False,
        celery=None,
    ):
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
                "id": jid(n, k),
                "s": study,
                "celery": celery,
                "status": status,
                "queued": BASE + timedelta(minutes=minute),
                "started": started,
                "finished": finished,
                "cancel": cancel,
                "worker": worker,
                "exit": exit_code,
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


def upgrade_0011(root: Path) -> None:
    """Upgrade to 0011 only: the 0011 tests check the state between stages 1 and 2."""
    alembic("upgrade", REV_AFTER, STORAGE_ROOT=str(root))


def test_0011_backfill_maps_legacy_rows(pg_engine, seeded):
    root = seeded
    upgrade_0011(root)
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
    assert (
        attrs["processor_name"] == "legacy-proc"
        and attrs["processor_version"] == "1.2.3"
    )
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
    expected = {
        1: "queued",
        2: "queued",
        3: "running",
        4: "succeeded",
        5: "succeeded",
        6: "failed",
        7: "canceled",
        8: "succeeded",
    }
    for n, status in expected.items():
        run = one(pg_engine, "SELECT * FROM pipeline_runs WHERE id = :i", i=sid(n))
        assert run["status"] == status, n
        assert run["case_id"] == sid(n)
        assert (
            run["pipeline_id"],
            run["pipeline_version"],
            run["pipeline_sha256"],
        ) == ("legacy", "0.0.0", None)
        assert (
            run["platform_version"],
            run["artefact_types_version"],
            run["input_set"],
            run["seed"],
        ) == ("legacy", "legacy", "legacy-single-file", 0)
        assert run["requested_implementations"] == {}
        assert run["accounting_submit_as"] == "service-account"
        assert run["accounting_billing_account"] is None
        assert run["accounting_submitted_by"] == f"user:{run['created_by_user_id']}"
        assert (run["finished_at"] is not None) == (
            status in ("succeeded", "failed", "canceled")
        )
    r3 = one(pg_engine, "SELECT * FROM pipeline_runs WHERE id = :i", i=sid(3))
    assert r3["started_at"] == (BASE + timedelta(minutes=5)).replace(
        tzinfo=timezone.utc
    )
    assert r3["resume_count"] == 1 and r3["failure_code"] is None
    r4 = one(pg_engine, "SELECT * FROM pipeline_runs WHERE id = :i", i=sid(4))
    assert r4["finished_at"] == (BASE + timedelta(minutes=40)).replace(
        tzinfo=timezone.utc
    )
    assert r4["resume_count"] == 1
    assert r4["legacy_clinical_review_status"] == "reviewed"
    r5 = one(pg_engine, "SELECT * FROM pipeline_runs WHERE id = :i", i=sid(5))
    assert r5["finished_at"] == (BASE + timedelta(hours=1)).replace(
        tzinfo=timezone.utc
    )  # updated_at
    assert r5["resume_count"] == 0
    r6 = one(pg_engine, "SELECT * FROM pipeline_runs WHERE id = :i", i=sid(6))
    assert r6["failure_code"] == "legacy_failure"
    assert r6["failure_message"] == "processing failed"
    r7 = one(pg_engine, "SELECT * FROM pipeline_runs WHERE id = :i", i=sid(7))
    assert r7["failure_code"] is None and r7["failure_message"] == "canceled by user"
    assert r7["cancel_requested_at"] == (BASE + timedelta(minutes=1)).replace(
        tzinfo=timezone.utc
    )
    assert r7["finished_at"] == (BASE + timedelta(hours=1)).replace(tzinfo=timezone.utc)

    # slots: one selected 'legacy' slot per run
    s4 = one(pg_engine, "SELECT * FROM run_slots WHERE pipeline_run_id = :i", i=sid(4))
    assert s4["id"] == derived(sid(4), "slot/legacy")
    assert (s4["slot"], s4["position"], s4["optional"], s4["status"]) == (
        "legacy",
        0,
        False,
        "selected",
    )
    assert (
        s4["implementation_id"],
        s4["step_name"],
        s4["step_version"],
        s4["stub"],
    ) == ("compneuro", "legacy-compneuro", "0.0.0", False)
    assert s4["image"] is None and s4["chosen_by"] == "deployment_default"
    assert s4["effective_parameters"] == {}
    s5 = one(pg_engine, "SELECT * FROM run_slots WHERE pipeline_run_id = :i", i=sid(5))
    assert s5["implementation_id"] == "dummy" and s5["stub"] is True
    s1 = one(pg_engine, "SELECT * FROM run_slots WHERE pipeline_run_id = :i", i=sid(1))
    assert s1["implementation_id"] == "unknown" and s1["step_name"] == "legacy-unknown"

    # step runs (processing_jobs -> attempts)
    j = {
        (n, k): one(pg_engine, "SELECT * FROM step_runs WHERE id = :i", i=jid(n, k))
        for n, k in ((2, 1), (3, 1), (3, 2), (4, 1), (4, 2), (5, 1), (6, 1), (7, 1))
    }
    assert (
        one(pg_engine, "SELECT count(*) AS n FROM step_runs WHERE legacy_job_id = id")[
            "n"
        ]
        == 8
    )
    assert j[(2, 1)]["status"] == "queued" and j[(2, 1)]["outcome"] is None
    assert j[(2, 1)]["attempt"] == 1 and j[(2, 1)]["trigger"] == "initial"
    assert j[(2, 1)]["executor"] == "subprocess" and j[(2, 1)]["runtime"] == "none"
    assert j[(2, 1)]["run_slot_id"] == derived(sid(2), "slot/legacy")
    assert j[(2, 1)]["pipeline_run_id"] == sid(2)
    # dead job: newer job exists -> failed, finished at the next job's queue time
    dead = j[(3, 1)]
    assert (dead["status"], dead["outcome"], dead["failure_code"]) == (
        "failed",
        "failed",
        "legacy_stale_job",
    )
    assert dead["finished_at"] == (BASE + timedelta(minutes=10)).replace(
        tzinfo=timezone.utc
    )
    live = j[(3, 2)]
    assert (live["status"], live["outcome"], live["attempt"], live["trigger"]) == (
        "running",
        None,
        2,
        "user_resume",
    )
    assert live["finished_at"] is None
    assert live["executor_state"] == {"worker_name": "worker-1"}
    assert j[(4, 1)]["status"] == "failed" and j[(4, 1)]["outcome"] == "failed"
    assert j[(4, 1)]["exit_code"] == 1 and j[(4, 1)]["failure_message"] == "boom"
    assert j[(4, 1)]["failure_code"] is None
    done = j[(4, 2)]
    assert (done["status"], done["outcome"], done["attempt"]) == (
        "succeeded",
        "succeeded",
        2,
    )
    assert done["log_path"] == f"{sid(4)}/logs/processing.log"
    assert done["orchestrator_task_id"] == "task-4-2" and done["exit_code"] == 0
    assert j[(5, 1)]["finished_at"] == (BASE + timedelta(minutes=3)).replace(
        tzinfo=timezone.utc
    )
    assert j[(7, 1)]["status"] == "canceled" and j[(7, 1)]["outcome"] == "canceled"
    assert j[(7, 1)]["cancel_requested_at"] == (BASE + timedelta(minutes=1)).replace(
        tzinfo=timezone.utc
    )
    assert j[(7, 1)]["finished_at"] == j[(7, 1)]["queued_at"]

    # artefacts and reports hang on the latest completed job
    arts = {
        a["port"]: a
        for a in rows(pg_engine, "SELECT * FROM artifacts WHERE case_id = :i", i=sid(4))
    }
    assert set(arts) == {"technical_report", "outputs_zip"}
    pdf = arts["technical_report"]
    assert pdf["id"] == derived(sid(4), "artifact/technical_report")
    assert pdf["step_run_id"] == jid(4, 2) and pdf["origin"] == "legacy"
    assert pdf["artefact_type"] == "legacy_technical_report_pdf"
    assert pdf["storage_path"] == f"{sid(4)}/output/report.pdf"
    assert (
        pdf["sha256"] is None
        and pdf["size_bytes"] is None
        and pdf["synthetic"] is False
    )
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
    upgrade_0011(seeded)
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
    upgrade_0011(seeded)
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE pipeline_runs SET status = 'failed', finished_at = now(),"
                " failure_code = 'step_failed' WHERE id = :i"
            ),
            {"i": sid(2)},
        )
    migration = load_migration_module()
    with pg_engine.begin() as conn:
        migration.backfill(conn, seeded)
    assert (
        one(pg_engine, "SELECT status FROM pipeline_runs WHERE id = :i", i=sid(2))[
            "status"
        ]
        == "failed"
    )


def test_0011_downgrade_restores_the_0010_schema(pg_engine, seeded):
    legacy_before = dump(pg_engine, LEGACY_TABLES)
    columns_before = {
        t: [c["name"] for c in inspect(pg_engine).get_columns(t)] for t in LEGACY_TABLES
    }
    upgrade_0011(seeded)
    assert one(pg_engine, "SELECT count(*) AS n FROM cases")["n"] == 8

    # downgrading is allowed: every case was back-filled from a study
    alembic("downgrade", REV_BEFORE)
    assert not (NEW_TABLES & user_tables(pg_engine))
    assert dump(pg_engine, LEGACY_TABLES) == legacy_before
    inspector = inspect(pg_engine)
    for table in LEGACY_TABLES:
        assert [c["name"] for c in inspector.get_columns(table)] == columns_before[
            table
        ]
    with pg_engine.connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == REV_BEFORE
    constraints = {c["name"] for c in inspector.get_check_constraints("users")}
    assert "ck_users_role" not in constraints

    # and the upgrade can be repeated on the downgraded database
    upgrade_0011(seeded)
    assert one(pg_engine, "SELECT count(*) AS n FROM cases")["n"] == 8
    assert one(pg_engine, "SELECT count(*) AS n FROM step_runs")["n"] == 8


def test_0011_downgrade_refuses_to_lose_new_data(pg_engine, seeded):
    upgrade_0011(seeded)
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

    alembic(
        "-x", "allow_data_loss=true", "downgrade", REV_BEFORE, STORAGE_ROOT=str(seeded)
    )
    assert not (NEW_TABLES & user_tables(pg_engine))
    assert one(pg_engine, "SELECT count(*) AS n FROM studies")["n"] == 8


def test_0011_aborts_and_lists_unmappable_legacy_rows(
    pg_engine, storage_root, tmp_path
):
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
    assert (
        str(sid(4)) in result.stderr and "more than one completed job" in result.stderr
    )
    # PostgreSQL DDL is transactional: nothing was created, the revision did not move
    assert not (NEW_TABLES & user_tables(pg_engine))
    with pg_engine.connect() as conn:
        assert (
            conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            == REV_BEFORE
        )


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
            {
                "i": study,
                "u": SYSTEM_USER_ID,
                "p": f"./test-data/studies/{study}/input/x.nii.gz",
                "t": BASE,
            },
        )
    alembic("upgrade", "head", STORAGE_ROOT="./test-data/studies")
    f = one(pg_engine, "SELECT * FROM input_files WHERE case_id = :i", i=study)
    assert f["storage_path"] == f"{study}/input/x.nii.gz"
    assert (
        one(pg_engine, "SELECT owner_user_id FROM cases WHERE id = :i", i=study)[
            "owner_user_id"
        ]
        == SYSTEM_USER_ID
    )


def test_0011_database_constraints(pg_engine, seeded):
    upgrade_0011(seeded)

    def fails(sql: str, **params) -> str:
        with pytest.raises(IntegrityError) as err:
            with pg_engine.begin() as conn:
                conn.execute(text(sql), params)
        return str(err.value)

    case_cols = (
        "id, owner_user_id, subject_label, deployment_profile, created_at, updated_at"
    )
    case_vals = "(:i, :u, :l, 'research-public', now(), now())"
    ok = uuid.uuid4()
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO cases ({case_cols}, sex, age_band) VALUES"
                " (:i, :u, :l, 'research-public', now(), now(), 'female', '90-104')"
            ),
            {"i": ok, "u": OWNER_A, "l": ok.hex[:12]},
        )
    bad = uuid.uuid4()
    base = {"i": bad, "u": OWNER_A, "l": bad.hex[:12]}
    assert "ck_cases_subject_label_format" in fails(
        f"INSERT INTO cases ({case_cols}) VALUES {case_vals}",
        **{**base, "l": "NOT-HEX"},
    )
    assert "uq_cases_subject_label" in fails(
        f"INSERT INTO cases ({case_cols}) VALUES {case_vals}",
        **{**base, "l": ok.hex[:12]},
    )
    assert "ck_cases_sex" in fails(
        f"INSERT INTO cases ({case_cols}, sex) VALUES (:i, :u, :l, 'research-public', now(),"
        " now(), 'other')",
        **base,
    )
    assert "ck_cases_age_band_format" in fails(
        f"INSERT INTO cases ({case_cols}, age_band) VALUES (:i, :u, :l, 'research-public',"
        " now(), now(), '65')",
        **base,
    )
    assert "ck_cases_deployment_profile" in fails(
        f"INSERT INTO cases ({case_cols}) VALUES (:i, :u, :l, 'bogus', now(), now())",
        **base,
    )

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
            " 'ct-pipeline', '1.0.0', :sha, '1.0', '1.0', 'ct-only', 7, '"
            + status
            + "',"
            " '{}'::jsonb, 'user:x', 'service-account', now(), now(), "
            + finished
            + extra_vals
            + ")"
        )

    sha = "a" * 64
    with pg_engine.begin() as conn:
        conn.execute(
            text(run_sql("succeeded", "now()")),
            {"r": uuid.uuid4(), "c": ok, "u": OWNER_A, "sha": sha},
        )
        conn.execute(
            text(run_sql("running", "NULL")),
            {"r": uuid.uuid4(), "c": ok, "u": OWNER_A, "sha": sha},
        )
    msg = fails(run_sql("queued", "NULL"), r=uuid.uuid4(), c=ok, u=OWNER_A, sha=sha)
    assert "uq_pipeline_runs_active_case" in msg
    assert "ck_pipeline_runs_finished_at" in fails(
        run_sql("succeeded", "NULL"), r=uuid.uuid4(), c=ok, u=OWNER_A, sha=sha
    )
    assert "ck_pipeline_runs_status" in fails(
        run_sql("done", "NULL"), r=uuid.uuid4(), c=sid(1), u=OWNER_A, sha=sha
    )
    assert "ck_pipeline_runs_pipeline_sha256" in fails(
        run_sql("succeeded", "now()"), r=uuid.uuid4(), c=sid(1), u=OWNER_A, sha=None
    )
    assert "ck_pipeline_runs_pipeline_version_format" in fails(
        run_sql("succeeded", "now()").replace("'1.0.0'", "'v1'"),
        r=uuid.uuid4(),
        c=sid(1),
        u=OWNER_A,
        sha=sha,
    )
    assert "ck_pipeline_runs_seed" in fails(
        run_sql("succeeded", "now()").replace(", 7,", ", -1,"),
        r=uuid.uuid4(),
        c=sid(1),
        u=OWNER_A,
        sha=sha,
    )

    # step runs: one success per slot, outcome agrees with status
    slot = derived(sid(2), "slot/legacy")
    step_sql = (
        "INSERT INTO step_runs (id, pipeline_run_id, run_slot_id, attempt, trigger, status,"
        " outcome, executor, runtime, queued_at, finished_at) VALUES (:i, :r, :s, :a,"
        " 'user_resume', :st, :o, 'subprocess', 'none', now(), :f)"
    )
    assert "ck_step_runs_status_outcome" in fails(
        step_sql,
        i=uuid.uuid4(),
        r=sid(2),
        s=slot,
        a=5,
        st="failed",
        o="succeeded",
        f=BASE,
    )
    assert "ck_step_runs_status_outcome" in fails(
        step_sql, i=uuid.uuid4(), r=sid(2), s=slot, a=5, st="failed", o=None, f=BASE
    )
    assert "uq_step_runs_one_active" in fails(
        step_sql, i=uuid.uuid4(), r=sid(2), s=slot, a=5, st="running", o=None, f=None
    )
    dup_slot = derived(sid(4), "slot/legacy")
    assert "uq_step_runs_one_success" in fails(
        step_sql,
        i=uuid.uuid4(),
        r=sid(4),
        s=dup_slot,
        a=5,
        st="succeeded",
        o="succeeded",
        f=BASE,
    )
    assert "uq_step_runs_slot_attempt" in fails(
        step_sql,
        i=uuid.uuid4(),
        r=sid(4),
        s=dup_slot,
        a=2,
        st="failed",
        o="failed",
        f=BASE,
    )
    msg = fails(
        step_sql.replace("'subprocess'", "'docker'"),
        i=uuid.uuid4(),
        r=sid(4),
        s=dup_slot,
        a=6,
        st="failed",
        o="failed",
        f=BASE,
    )
    assert "ck_step_runs_image" in msg or "ck_step_runs_granted_resources" in msg

    # run slots: a selected slot needs its implementation; others need a reason
    slot_sql = (
        "INSERT INTO run_slots (id, pipeline_run_id, slot, position, optional, status, reason)"
        " VALUES (:i, :r, :s, :p, false, :st, :re)"
    )
    assert "ck_run_slots_selected" in fails(
        slot_sql, i=uuid.uuid4(), r=sid(2), s="norm", p=1, st="selected", re=None
    )
    assert "ck_run_slots_selected" in fails(
        slot_sql, i=uuid.uuid4(), r=sid(2), s="norm", p=1, st="skipped", re=None
    )
    with pg_engine.begin() as conn:
        conn.execute(
            text(slot_sql),
            dict(
                i=uuid.uuid4(),
                r=sid(2),
                s="norm",
                p=1,
                st="skipped",
                re="condition_false",
            ),
        )

    # roles
    user_sql = (
        "INSERT INTO users (id, email, full_name, role, is_active, created_at, updated_at,"
        " notify_on_processing_completed, notify_on_processing_failed)"
        " VALUES (:i, :e, 'T', :r, true, now(), now(), true, true)"
    )
    for role in ("reviewer", "coordinator", "service"):
        with pg_engine.begin() as conn:
            conn.execute(
                text(user_sql), dict(i=uuid.uuid4(), e=f"{role}@example.org", r=role)
            )
    assert "ck_users_role" in fails(
        user_sql, i=uuid.uuid4(), e="x@example.org", r="superuser"
    )

    # settings: a default must be enabled and unique per slot
    set_sql = (
        "INSERT INTO slot_implementation_settings (id, pipeline_id, slot, implementation_id,"
        " enabled, is_default, updated_at) VALUES (:i, 'ct-pipeline', 'norm', :impl, :en, :d, now())"
    )
    assert "ck_slot_impl_settings_default_enabled" in fails(
        set_sql, i=uuid.uuid4(), impl="a", en=False, d=True
    )
    with pg_engine.begin() as conn:
        conn.execute(text(set_sql), dict(i=uuid.uuid4(), impl="a", en=True, d=True))
    assert "uq_slot_impl_settings_default" in fails(
        set_sql, i=uuid.uuid4(), impl="b", en=True, d=True
    )

    # legacy tables keep accepting rows written by the legacy code (no new column needed)
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO audit_events (id, study_id, event_type, timestamp, actor)"
                " VALUES (:i, :s, 'x', now(), 'a')"
            ),
            {"i": uuid.uuid4(), "s": sid(1)},
        )
        conn.execute(
            text(
                "INSERT INTO share_links (id, study_id, created_by_user_id, token_hash,"
                " expires_at, created_at, access_count)"
                " VALUES (:i, :s, :u, :h, now(), now(), 0)"
            ),
            {"i": uuid.uuid4(), "s": sid(1), "u": OWNER_A, "h": "e" * 64},
        )


def test_0011_schema_matches_the_orm_models(pg_engine):
    """The migration and the SQLAlchemy models must describe the same tables."""
    from app.db.base import Base
    import app.models  # noqa: F401  (registers the tables)

    alembic("upgrade", "head")
    inspector = inspect(pg_engine)
    tables = NEW_TABLES | {
        "users",
        "audit_events",
        "notifications",
        "share_links",
        "preset_settings",  # added by 0013
    }
    for name in sorted(tables):
        table = Base.metadata.tables[name]
        db_cols = {c["name"]: c for c in inspector.get_columns(name)}
        assert set(db_cols) == {c.name for c in table.columns}, name
        for column in table.columns:
            assert db_cols[column.name]["nullable"] == (
                column.nullable and not column.primary_key
            ), (name, column.name)
        db_indexes = {i["name"] for i in inspector.get_indexes(name)}
        orm_indexes = {i.name for i in table.indexes}
        assert orm_indexes <= db_indexes, (name, orm_indexes - db_indexes)
        db_checks = {c["name"] for c in inspector.get_check_constraints(name)}
        orm_checks = {
            c.name
            for c in table.constraints
            if c.__class__.__name__ == "CheckConstraint" and c.name
        }
        assert orm_checks <= db_checks, (name, orm_checks - db_checks)
        db_uniques = {u["name"] for u in inspector.get_unique_constraints(name)}
        orm_uniques = {
            c.name
            for c in table.constraints
            if c.__class__.__name__ == "UniqueConstraint" and c.name
        }
        assert orm_uniques <= db_uniques, (name, orm_uniques - db_uniques)
    # every regex format CHECK of the migration targets an existing column
    migration = load_migration_module()
    for table, column, _pattern in migration.FORMAT_CHECKS:
        assert column in Base.metadata.tables[table].columns
        names = {c["name"] for c in inspector.get_check_constraints(table)}
        assert f"ck_{table}_{column}_format" in names
    path = BACKEND_DIR / "alembic" / "versions" / "0013_run_plans.py"
    spec = importlib.util.spec_from_file_location("migration_0013", path)
    plans = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plans)
    for table, column, _pattern in plans.FORMAT_CHECKS:
        assert column in Base.metadata.tables[table].columns
        names = {c["name"] for c in inspector.get_check_constraints(table)}
        assert f"ck_{table}_{column}_format" in names


# ---------------------------------------------------------------------------------------
# Migration 0012 (legacy cut-over): final reconciliation, NOT NULL share_links.case_id and
# the downgrade paths of docs/data-model.md 10.5.
# ---------------------------------------------------------------------------------------

REV_CUTOVER = "0012_legacy_cutover"
LINKED_TABLES = ["audit_events", "notifications", "share_links"]


def load_cutover_module():
    path = BACKEND_DIR / "alembic" / "versions" / "0012_legacy_cutover.py"
    spec = importlib.util.spec_from_file_location("migration_0012", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reconcile(engine, root: Path, dry_run: bool) -> dict:
    module = load_cutover_module()
    with engine.begin() as conn:
        return module.reconcile(conn, root, dry_run=dry_run)


def nullable(engine, table: str, column: str) -> bool:
    return {c["name"]: c["nullable"] for c in inspect(engine).get_columns(table)}[column]


def version(engine) -> str:
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


def test_0012_reconciles_lagging_mirror_rows(pg_engine, seeded):
    """Mirror rows that lagged behind the legacy tables (stage 1b) are brought back to
    the mapping of 10.3; a dry run reports the work and writes nothing; a second run
    finds nothing to do."""
    upgrade_0011(seeded)
    tables = sorted(NEW_TABLES) + LINKED_TABLES
    reference = dump(pg_engine, tables)
    with pg_engine.begin() as conn:
        # a mirrored run whose state differs from its study
        conn.execute(
            text(
                "UPDATE pipeline_runs SET status = 'failed', finished_at = now(),"
                " failure_code = 'legacy_failure' WHERE id = :i"
            ),
            {"i": sid(2)},
        )
        # an attempt renumbered by the mirror, its artefacts and report lost, and
        # dependent rows not linked yet
        conn.execute(
            text("DELETE FROM reports WHERE pipeline_run_id = :i"), {"i": sid(4)}
        )
        conn.execute(
            text("DELETE FROM artifacts WHERE pipeline_run_id = :i"), {"i": sid(4)}
        )
        conn.execute(
            text("UPDATE step_runs SET attempt = 7 WHERE id = :i"), {"i": jid(4, 2)}
        )
        conn.execute(
            text("UPDATE audit_events SET case_id = NULL WHERE study_id = :i"),
            {"i": sid(4)},
        )
        conn.execute(
            text("UPDATE share_links SET case_id = NULL WHERE study_id = :i"),
            {"i": sid(4)},
        )
        # a study whose mirror never ran
        for table in ("reports", "artifacts", "step_runs", "run_slots", "run_inputs"):
            conn.execute(
                text(f"DELETE FROM {table} WHERE pipeline_run_id = :i"), {"i": sid(5)}
            )
        conn.execute(text("DELETE FROM pipeline_runs WHERE id = :i"), {"i": sid(5)})
        conn.execute(text("DELETE FROM input_files WHERE case_id = :i"), {"i": sid(5)})
        conn.execute(text("DELETE FROM cases WHERE id = :i"), {"i": sid(5)})
    tampered = dump(pg_engine, tables)

    report = reconcile(pg_engine, seeded, dry_run=True)
    assert report["studies"] == 8 and report["dry_run"] is True
    assert report["cases"] == {"insert": 1, "update": 0, "unchanged": 7}
    assert report["pipeline_runs"] == {"insert": 1, "update": 1, "unchanged": 6}
    assert report["attempt_sets"] == {"rewrite": 2, "unchanged": 6}
    assert report["linked_to_case"]["share_links"] == 1
    assert report["linked_to_case"]["audit_events"] == 2
    assert dump(pg_engine, tables) == tampered  # a dry run writes nothing

    alembic("upgrade", REV_CUTOVER, STORAGE_ROOT=str(seeded))
    assert dump(pg_engine, tables) == reference
    assert not nullable(pg_engine, "share_links", "case_id")
    assert nullable(pg_engine, "share_links", "study_id")

    again = reconcile(pg_engine, seeded, dry_run=False)
    for table in ("cases", "input_files", "pipeline_runs", "run_inputs", "run_slots"):
        assert again[table]["insert"] == 0 and again[table]["update"] == 0, table
    assert again["attempt_sets"] == {"rewrite": 0, "unchanged": 8}
    assert set(again["linked_to_case"].values()) == {0}
    assert dump(pg_engine, tables) == reference


def test_0012_aborts_and_lists_unmappable_rows(pg_engine, seeded):
    upgrade_0011(seeded)
    stray = uuid.uuid4()
    with pg_engine.begin() as conn:
        # an attempt that no legacy job explains
        conn.execute(
            text(
                "INSERT INTO step_runs (id, pipeline_run_id, run_slot_id, attempt, trigger,"
                " status, outcome, executor, runtime, queued_at, finished_at) VALUES"
                " (:i, :r, :s, 9, 'user_resume', 'failed', 'failed', 'subprocess', 'none',"
                " now(), now())"
            ),
            {"i": stray, "r": sid(6), "s": derived(sid(6), "slot/legacy")},
        )
        # a study with two completed jobs
        conn.execute(
            text(
                "INSERT INTO processing_jobs (id, study_id, status, queued_at, retry_count)"
                " VALUES (:i, :s, 'completed', :t, 0)"
            ),
            {"i": jid(4, 9), "s": sid(4), "t": BASE + timedelta(hours=2)},
        )
    failed = alembic_run("upgrade", "head", STORAGE_ROOT=str(seeded))
    assert failed.returncode != 0
    assert str(stray) in failed.stderr and "has no legacy job" in failed.stderr
    assert str(sid(4)) in failed.stderr and "more than one completed job" in failed.stderr
    # nothing was applied
    assert version(pg_engine) == REV_AFTER
    assert nullable(pg_engine, "share_links", "case_id")


def test_0012_round_trip_without_activity_restores_the_legacy_tables(pg_engine, seeded):
    legacy_before = dump(pg_engine, LEGACY_TABLES)
    upgrade_0011(seeded)
    at_0011 = dump(pg_engine, sorted(NEW_TABLES) + LEGACY_TABLES)
    alembic("upgrade", REV_CUTOVER, STORAGE_ROOT=str(seeded))
    alembic("downgrade", REV_AFTER, STORAGE_ROOT=str(seeded))
    assert version(pg_engine) == REV_AFTER
    assert not nullable(pg_engine, "share_links", "study_id")
    assert nullable(pg_engine, "share_links", "case_id")
    assert dump(pg_engine, sorted(NEW_TABLES) + LEGACY_TABLES) == at_0011
    alembic("downgrade", REV_BEFORE, STORAGE_ROOT=str(seeded))
    assert dump(pg_engine, LEGACY_TABLES) == legacy_before


def _post_cutover_activity(engine) -> dict:
    """What the aliases and the worker write after the cut-over, as plain SQL."""
    t0 = datetime(2026, 11, 20, 9, 0, 0, tzinfo=timezone.utc)
    naive = t0.replace(tzinfo=None)
    ids = {
        "retry": uuid.uuid4(),
        "alias": uuid.uuid4(),
        "alias_step": uuid.uuid4(),
        "v1": uuid.uuid4(),
        "t0": naive,
    }
    alias, v1 = ids["alias"], ids["v1"]
    with engine.begin() as conn:
        # (a) study 6 failed before the cut-over; it is retried and succeeds afterwards
        conn.execute(
            text(
                "UPDATE pipeline_runs SET status = 'succeeded', finished_at = :t,"
                " started_at = :t, failure_code = NULL, failure_message = NULL,"
                " resume_count = 1, updated_at = :t,"
                " legacy_clinical_review_status = 'validated' WHERE id = :i"
            ),
            {"i": sid(6), "t": t0},
        )
        conn.execute(
            text(
                "INSERT INTO step_runs (id, pipeline_run_id, run_slot_id, attempt, trigger,"
                " status, outcome, executor, runtime, queued_at, started_at, finished_at,"
                " exit_code, orchestrator_task_id, executor_state, log_path) VALUES"
                " (:i, :r, :s, 2, 'user_resume', 'succeeded', 'succeeded', 'subprocess',"
                " 'none', :t, :t, :t, 0, 'celery-retry', CAST(:state AS jsonb), :log)"
            ),
            {
                "i": ids["retry"],
                "r": sid(6),
                "s": derived(sid(6), "slot/legacy"),
                "t": t0,
                "state": json.dumps({"worker_name": "w2"}),
                "log": f"{sid(6)}/logs/processor.log",
            },
        )
        art = derived(sid(6), "artifact/technical_report")
        conn.execute(
            text(
                "INSERT INTO artifacts (id, case_id, pipeline_run_id, step_run_id, origin,"
                " port, artefact_type, storage_path, synthetic, created_at, size_bytes,"
                " sha256) VALUES (:a, :c, :c, :s, 'legacy', 'technical_report',"
                " 'legacy_technical_report_pdf', :p, false, :t, 10, :h)"
            ),
            {
                "a": art,
                "c": sid(6),
                "s": ids["retry"],
                "p": f"{sid(6)}/output/reports/technical_report.pdf",
                "t": t0,
                "h": "b" * 64,
            },
        )
        conn.execute(
            text(
                "INSERT INTO reports (id, case_id, pipeline_run_id, step_run_id,"
                " pdf_artifact_id, template, language, created_at) VALUES (:i, :c, :c, :s,"
                " :a, 'legacy-technical', 'es', :t)"
            ),
            {
                "i": derived(sid(6), "report"),
                "c": sid(6),
                "s": ids["retry"],
                "a": art,
                "t": t0,
            },
        )
        # (b) a study uploaded through the alias after the cut-over
        conn.execute(
            text(
                "INSERT INTO cases (id, owner_user_id, subject_label, deployment_profile,"
                " created_at, updated_at, legacy_attributes) VALUES (:i, :u, :l, 'legacy',"
                " :t, :t, CAST(:attrs AS jsonb))"
            ),
            {
                "i": alias,
                "u": OWNER_A,
                "l": alias.hex[:12],
                "t": t0,
                "attrs": json.dumps(
                    {
                        "original_filename": "new-upload.nii.gz",
                        "processor_backend": "dummy",
                        "output_path": f"{alias}/output",
                        "processing_warnings": None,
                    }
                ),
            },
        )
        conn.execute(
            text(
                "INSERT INTO input_files (id, case_id, role, artefact_type, storage_path,"
                " sha256, size_bytes, uploaded_by_user_id, created_at) VALUES (:i, :c,"
                " 'legacy_upload', 'legacy_upload', :p, :h, 42, :u, :t)"
            ),
            {
                "i": derived(alias, "input"),
                "c": alias,
                "p": f"{alias}/input/original/new-upload.nii.gz",
                "h": "c" * 64,
                "u": OWNER_A,
                "t": t0,
            },
        )
        conn.execute(
            text(
                "INSERT INTO pipeline_runs (id, case_id, created_by_user_id, pipeline_id,"
                " pipeline_version, platform_version, artefact_types_version, input_set,"
                " seed, status, requested_implementations, accounting_submitted_by,"
                " accounting_submit_as, created_at, updated_at, started_at, finished_at,"
                " legacy_clinical_review_status) VALUES (:i, :i, :u, 'legacy', '0.0.0',"
                " 'legacy', 'legacy', 'legacy-single-file', 0, 'succeeded', '{}'::jsonb,"
                " :acc, 'service-account', :t, :t, :t, :t, 'technical_only')"
            ),
            {"i": alias, "u": OWNER_A, "acc": f"user:{OWNER_A}", "t": t0},
        )
        conn.execute(
            text(
                "INSERT INTO run_inputs (pipeline_run_id, role, input_file_id)"
                " VALUES (:r, 'legacy_upload', :f)"
            ),
            {"r": alias, "f": derived(alias, "input")},
        )
        conn.execute(
            text(
                "INSERT INTO run_slots (id, pipeline_run_id, slot, position, optional,"
                " status, implementation_id, step_name, step_version, stub, chosen_by,"
                " effective_parameters) VALUES (:i, :r, 'legacy', 0, false, 'selected',"
                " 'dummy', 'legacy-dummy', '0.0.0', true, 'deployment_default',"
                " '{}'::jsonb)"
            ),
            {"i": derived(alias, "slot/legacy"), "r": alias},
        )
        conn.execute(
            text(
                "INSERT INTO step_runs (id, pipeline_run_id, run_slot_id, attempt, trigger,"
                " status, outcome, executor, runtime, queued_at, started_at, finished_at,"
                " exit_code, orchestrator_task_id) VALUES (:i, :r, :s, 1, 'initial',"
                " 'succeeded', 'succeeded', 'subprocess', 'none', :t, :t, :t, 0,"
                " 'celery-a')"
            ),
            {
                "i": ids["alias_step"],
                "r": alias,
                "s": derived(alias, "slot/legacy"),
                "t": t0,
            },
        )
        conn.execute(
            text(
                "INSERT INTO audit_events (id, case_id, pipeline_run_id, event_type,"
                " timestamp, actor) VALUES (:i, :c, :c, 'study_uploaded', :t,"
                " 'a@example.org')"
            ),
            {"i": uuid.uuid4(), "c": alias, "t": naive},
        )
        # (c) a case created through /api/v1; links of (b) and (c) have no study
        conn.execute(
            text(
                "INSERT INTO cases (id, owner_user_id, subject_label, deployment_profile,"
                " created_at, updated_at) VALUES (:i, :u, :l, 'research-public', :t, :t)"
            ),
            {"i": v1, "u": OWNER_B, "l": v1.hex[:12], "t": t0},
        )
        for case_id, owner, token in ((alias, OWNER_A, "1"), (v1, OWNER_B, "2")):
            conn.execute(
                text(
                    "INSERT INTO share_links (id, case_id, created_by_user_id, token_hash,"
                    " expires_at, created_at, access_count) VALUES (:i, :c, :u, :h, :t,"
                    " :t, 0)"
                ),
                {"i": uuid.uuid4(), "c": case_id, "u": owner, "h": token * 64, "t": naive},
            )
    return ids


def test_0012_downgrade_writes_back_post_cutover_activity(pg_engine, seeded):
    root = seeded
    upgrade_0011(root)
    alembic("upgrade", REV_CUTOVER, STORAGE_ROOT=str(root))
    s4_before = one(pg_engine, "SELECT * FROM studies WHERE id = :i", i=sid(4))
    jobs4_before = rows(
        pg_engine,
        "SELECT * FROM processing_jobs WHERE study_id = :i ORDER BY id",
        i=sid(4),
    )
    ids = _post_cutover_activity(pg_engine)
    t0 = ids["t0"]

    alembic("downgrade", REV_AFTER, STORAGE_ROOT=str(root))
    assert version(pg_engine) == REV_AFTER

    # (a) the retried study gets its new state and a job for the new attempt
    s6 = one(pg_engine, "SELECT * FROM studies WHERE id = :i", i=sid(6))
    assert s6["status"] == "completed"
    assert s6["error_message"] is None
    assert s6["clinical_review_status"] == "validated"
    assert s6["processing_started_at"] == t0 and s6["processing_finished_at"] == t0
    assert s6["pdf_path"] == str(
        root / str(sid(6)) / "output" / "reports" / "technical_report.pdf"
    )
    assert s6["updated_at"] == t0
    job = one(pg_engine, "SELECT * FROM processing_jobs WHERE id = :i", i=ids["retry"])
    assert (job["study_id"], job["status"], job["retry_count"]) == (
        sid(6),
        "completed",
        1,
    )
    assert job["celery_task_id"] == "celery-retry" and job["worker_name"] == "w2"
    assert job["log_path"] == str(root / str(sid(6)) / "logs" / "processor.log")
    linked_job = one(
        pg_engine, "SELECT legacy_job_id FROM step_runs WHERE id = :i", i=ids["retry"]
    )
    assert linked_job["legacy_job_id"] == ids["retry"]
    assert (
        one(pg_engine, "SELECT status FROM processing_jobs WHERE id = :i", i=jid(6, 1))[
            "status"
        ]
        == "failed"
    )
    # an untouched study and its jobs are bit for bit what they were
    assert one(pg_engine, "SELECT * FROM studies WHERE id = :i", i=sid(4)) == s4_before
    assert (
        rows(
            pg_engine,
            "SELECT * FROM processing_jobs WHERE study_id = :i ORDER BY id",
            i=sid(4),
        )
        == jobs4_before
    )

    # (b) the alias upload is a study again, linked from its case and dependent rows
    alias = ids["alias"]
    study = one(pg_engine, "SELECT * FROM studies WHERE id = :i", i=alias)
    assert study["original_filename"] == "new-upload.nii.gz"
    assert study["status"] == "completed" and study["processor_backend"] == "dummy"
    assert study["stored_path"] == str(
        root / str(alias) / "input" / "original" / "new-upload.nii.gz"
    )
    assert study["file_size"] == 42 and study["checksum"] == "c" * 64
    assert study["created_at"] == t0
    job = one(pg_engine, "SELECT * FROM processing_jobs WHERE study_id = :i", i=alias)
    assert job["id"] == ids["alias_step"] and job["status"] == "completed"
    case = one(pg_engine, "SELECT legacy_study_id FROM cases WHERE id = :i", i=alias)
    assert case["legacy_study_id"] == alias
    event = one(pg_engine, "SELECT study_id FROM audit_events WHERE case_id = :i", i=alias)
    assert event["study_id"] == alias
    link = one(pg_engine, "SELECT study_id FROM share_links WHERE case_id = :i", i=alias)
    assert link["study_id"] == alias

    # (c) the v1 case stays; (d) its share link is deleted
    assert (
        one(pg_engine, "SELECT count(*) AS n FROM cases WHERE id = :i", i=ids["v1"])["n"]
        == 1
    )
    assert rows(pg_engine, "SELECT * FROM share_links WHERE case_id = :i", i=ids["v1"]) == []
    assert not nullable(pg_engine, "share_links", "study_id")

    # the cut-over can be applied again on the downgraded database
    alembic("upgrade", REV_CUTOVER, STORAGE_ROOT=str(root))
    assert (
        one(pg_engine, "SELECT status FROM pipeline_runs WHERE id = :i", i=alias)["status"]
        == "succeeded"
    )
    steps = rows(
        pg_engine,
        "SELECT id, attempt, trigger, status FROM step_runs WHERE pipeline_run_id = :i"
        " ORDER BY attempt",
        i=sid(6),
    )
    assert [(r["id"], r["attempt"], r["trigger"], r["status"]) for r in steps] == [
        (jid(6, 1), 1, "initial", "failed"),
        (ids["retry"], 2, "user_resume", "succeeded"),
    ]


def test_0012_dry_run_cli(pg_engine, seeded):
    upgrade_0011(seeded)
    env = {
        **os.environ,
        "DATABASE_URL": os.environ["TEST_POSTGRES_URL"],
        "STORAGE_ROOT": str(seeded),
    }
    command = [sys.executable, "-m", "app.cli.legacy_reconcile"]
    ok = subprocess.run(command, cwd=BACKEND_DIR, env=env, capture_output=True, text=True)
    assert ok.returncode == 0, ok.stderr
    report = json.loads(ok.stdout)
    assert report["studies"] == 8 and report["dry_run"] is True
    assert report["database_revision"] == REV_AFTER
    assert report["attempt_sets"] == {"rewrite": 0, "unchanged": 8}
    assert version(pg_engine) == REV_AFTER

    with pg_engine.begin() as conn:
        conn.execute(
            text("UPDATE studies SET pdf_path = '/elsewhere/report.pdf' WHERE id = :i"),
            {"i": sid(5)},
        )
    refused = subprocess.run(
        command, cwd=BACKEND_DIR, env=env, capture_output=True, text=True
    )
    assert refused.returncode == 1
    assert str(sid(5)) in refused.stderr and "outside the storage root" in refused.stderr


def test_0012_fills_sizes_of_backfilled_legacy_artefacts(pg_engine, seeded):
    """Back-filled legacy artefacts get the size of their file so that the quota
    (inputs plus derivatives) counts them; a missing file leaves the size NULL. The
    reconciliation does not compare sizes, so a re-run changes nothing."""
    pdf = seeded / str(sid(4)) / "output" / "report.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 91)  # the ZIP of study 4 stays missing
    upgrade_0011(seeded)
    report = load_cutover_module().fill_legacy_artifact_sizes
    with pg_engine.begin() as conn:
        # study 4's PDF is found; its ZIP and study 5's PDF are missing
        assert report(conn, seeded, dry_run=True) == {
            "filled": 1,
            "missing_file": 2,
            "dry_run": True,
        }
    alembic("upgrade", REV_CUTOVER, STORAGE_ROOT=str(seeded))
    sizes = {
        r["port"]: r["size_bytes"]
        for r in rows(
            pg_engine,
            "SELECT port, size_bytes FROM artifacts WHERE pipeline_run_id = :i",
            i=sid(4),
        )
    }
    assert sizes == {"technical_report": 100, "outputs_zip": None}
    again = reconcile(pg_engine, seeded, dry_run=False)
    assert again["attempt_sets"]["rewrite"] == 0
    assert (
        one(
            pg_engine,
            "SELECT size_bytes FROM artifacts WHERE pipeline_run_id = :i"
            " AND port = 'technical_report'",
            i=sid(4),
        )["size_bytes"]
        == 100
    )


# ---------------------------------------------------------------------------------------
# Migration 0013 (run plans and preset settings): additive; existing rows keep their
# meaning; the plan role of existing runs is back-filled; the downgrade refuses to lose
# plan data (docs/design/run-plans.md 3.7).
# ---------------------------------------------------------------------------------------

REV_PLANS = "0013_run_plans"
PRESET_SHA = "c" * 64
RUN_IMAGE = "registry.example.org/team/step@sha256:" + "b" * 64
PIPELINE_CASE = uuid.UUID("cccccccc-0000-4000-8000-000000000001")
SECOND_CASE = uuid.UUID("cccccccc-0000-4000-8000-000000000002")
PIPELINE_RUN = uuid.UUID("dddddddd-0000-4000-8000-000000000001")
FAILED_RUN = uuid.UUID("dddddddd-0000-4000-8000-000000000002")
PLAN_TABLES = ("pipeline_runs", "run_slots", "input_files")


def _table_columns(engine, table: str) -> list[str]:
    return [c["name"] for c in inspect(engine).get_columns(table)]


def _check_names(engine, table: str) -> set[str]:
    return {c["name"] for c in inspect(engine).get_check_constraints(table)}


def _snapshot(engine, columns: dict[str, list[str]]) -> dict[str, list[tuple]]:
    """Rows of the given columns, as they are before and after the migration."""
    out = {}
    with engine.connect() as conn:
        for table, cols in columns.items():
            sql = f"SELECT {', '.join(cols)} FROM {table} ORDER BY {', '.join(cols[:2])}"
            out[table] = [tuple(r) for r in conn.execute(text(sql)).all()]
    return out


def _insert_case(conn, case_id) -> None:
    conn.execute(
        text(
            "INSERT INTO cases (id, owner_user_id, subject_label, deployment_profile,"
            " created_at, updated_at) VALUES (:i, :u, :l, 'research-public', now(), now())"
        ),
        {"i": case_id, "u": OWNER_A, "l": case_id.hex[-12:]},
    )


def _insert_plan_run(conn, run_id, case_id, status="succeeded") -> None:
    finished = status in ("succeeded", "failed", "canceled")
    conn.execute(
        text(
            "INSERT INTO pipeline_runs (id, case_id, created_by_user_id, pipeline_id,"
            " pipeline_version, pipeline_sha256, platform_version, artefact_types_version,"
            " input_set, seed, status, requested_implementations, accounting_submitted_by,"
            " accounting_submit_as, created_at, updated_at, finished_at) VALUES (:i, :c,"
            " :u, 'stroke-lnm', '0.1.0', :sha, '1.0', '1.0', 'ct-and-lesion', 7, :s,"
            " '{}'::jsonb, 'user:x', 'service-account', now(), now(), "
            + ("now()" if finished else "NULL")
            + ")"
        ),
        {"i": run_id, "c": case_id, "u": OWNER_A, "sha": "a" * 64, "s": status},
    )


def _insert_slot(conn, run_id, slot, position, optional, kind, **extra) -> None:
    """kind: 'selected', or a reason (condition_false, not_requested, ...)."""
    values = {
        "id": uuid.uuid4(),
        "pipeline_run_id": run_id,
        "slot": slot,
        "position": position,
        "optional": optional,
    }
    marks = {k: f":{k}" for k in values}
    if kind == "selected":
        values.update(
            status="selected",
            implementation_id="synthetic-stub",
            step_name="step-a",
            step_version="1.0.0",
            image=RUN_IMAGE,
            stub=True,
            chosen_by="pipeline_default",
        )
        marks = {k: f":{k}" for k in values}
        marks["effective_parameters"] = "CAST('{}' AS jsonb)"
    else:
        values.update(
            status="unavailable" if kind == "no_implementation" else "skipped",
            reason=kind,
        )
        marks = {k: f":{k}" for k in values}
    for key, value in extra.items():
        values[key] = value
        marks[key] = f"CAST(:{key} AS jsonb)" if key == "upstream" else f":{key}"
    conn.execute(
        text(
            f"INSERT INTO run_slots ({', '.join(marks)}) VALUES ({', '.join(marks.values())})"
        ),
        values,
    )


class _Reported(str):
    """The constraint a violation names. ``name in reported`` is an exact comparison, so
    that ``ck_run_slots_plan_role`` does not match ``ck_run_slots_plan_role_reason``."""

    def __contains__(self, other) -> bool:
        return str.__eq__(self, other)


def _violation(engine, sql: str, **params) -> str:
    with pytest.raises(IntegrityError) as err:
        with engine.begin() as conn:
            conn.execute(text(sql), params)
    message = str(err.value)
    match = re.search(r'constraint "(\w+)"', message)
    return _Reported(match.group(1)) if match else message


@pytest.fixture()
def at_0012(pg_engine, seeded):
    """A 0012 database: the legacy seed (cut over) plus pipeline runs of every slot kind."""
    alembic("upgrade", REV_CUTOVER, STORAGE_ROOT=str(seeded))
    with pg_engine.begin() as conn:
        _insert_case(conn, PIPELINE_CASE)
        conn.execute(
            text(
                "INSERT INTO input_files (id, case_id, role, artefact_type, storage_path,"
                " sha256, size_bytes, uploaded_by_user_id, created_at) VALUES (:i, :c,"
                " 'ct', 'ct_native', 'sub-x/ct.nii.gz', :sha, 10, :u, now())"
            ),
            {"i": uuid.uuid4(), "c": PIPELINE_CASE, "sha": "e" * 64, "u": OWNER_A},
        )
        _insert_plan_run(conn, PIPELINE_RUN, PIPELINE_CASE)
        _insert_slot(conn, PIPELINE_RUN, "lesion-intake", 0, False, "selected")
        _insert_slot(conn, PIPELINE_RUN, "qc-report", 1, True, "selected")
        _insert_slot(conn, PIPELINE_RUN, "mni-only", 2, False, "condition_false")
        _insert_slot(conn, PIPELINE_RUN, "optional-a", 3, True, "upstream_absent")
        _insert_slot(conn, PIPELINE_RUN, "optional-b", 4, True, "no_implementation")
        # a failed run of a second case: a slot chosen by the user
        _insert_case(conn, SECOND_CASE)
        _insert_plan_run(conn, FAILED_RUN, SECOND_CASE, status="failed")
        _insert_slot(
            conn, FAILED_RUN, "lesion-intake", 0, False, "selected", chosen_by="user"
        )
    return seeded


def _plan_roles(engine) -> dict[tuple[str, str], str | None]:
    return {
        (str(r["pipeline_run_id"]), r["slot"]): r["plan_role"]
        for r in rows(engine, "SELECT pipeline_run_id, slot, plan_role FROM run_slots")
    }


def test_0013_is_additive_and_backfills_plan_roles(pg_engine, at_0012):
    old_columns = {t: _table_columns(pg_engine, t) for t in PLAN_TABLES}
    old_tables = user_tables(pg_engine)
    before = _snapshot(pg_engine, old_columns)
    assert before["pipeline_runs"] and before["run_slots"] and before["input_files"]
    other = sorted(set(NEW_TABLES) - set(PLAN_TABLES)) + LINKED_TABLES
    reference = dump(pg_engine, other)

    alembic("upgrade", REV_PLANS, STORAGE_ROOT=str(at_0012))
    assert version(pg_engine) == REV_PLANS

    # nothing that existed changed: same tables (plus one), same values in the old columns
    assert user_tables(pg_engine) == old_tables | {"preset_settings"}
    assert _snapshot(pg_engine, old_columns) == before
    assert dump(pg_engine, other) == reference

    # every existing run is a full-plan run with no preset and no targets
    runs = rows(pg_engine, "SELECT * FROM pipeline_runs")
    assert len(runs) == len(before["pipeline_runs"])
    for run in runs:
        assert run["plan_mode"] == "full"
        assert run["preset_id"] is None and run["preset_version"] is None
        assert run["preset_sha256"] is None
        assert run["targets"] == {}
    assert rows(pg_engine, "SELECT 1 FROM input_files WHERE origin IS NOT NULL") == []
    assert rows(pg_engine, "SELECT 1 FROM run_slots WHERE upstream IS NOT NULL") == []
    assert rows(pg_engine, "SELECT 1 FROM preset_settings") == []

    # plan roles: non-optional applicable -> target; optional or not selected ->
    # if_available; condition_false and legacy rows stay NULL
    roles = _plan_roles(pg_engine)
    run = str(PIPELINE_RUN)
    assert roles[(run, "lesion-intake")] == "target"
    assert roles[(run, "qc-report")] == "if_available"
    assert roles[(run, "mni-only")] is None
    assert roles[(run, "optional-a")] == "if_available"
    assert roles[(run, "optional-b")] == "if_available"
    assert roles[(str(FAILED_RUN), "lesion-intake")] == "target"
    legacy = rows(
        pg_engine,
        "SELECT s.plan_role FROM run_slots s JOIN pipeline_runs r ON r.id = s.pipeline_run_id"
        " WHERE r.pipeline_id = 'legacy'",
    )
    assert legacy and all(r["plan_role"] is None for r in legacy)


def test_0013_new_constraints(pg_engine, at_0012):
    alembic("upgrade", REV_PLANS, STORAGE_ROOT=str(at_0012))
    base_cols = (
        "id, case_id, created_by_user_id, pipeline_id, pipeline_version, pipeline_sha256,"
        " platform_version, artefact_types_version, input_set, seed, status,"
        " requested_implementations, accounting_submitted_by, accounting_submit_as,"
        " created_at, updated_at, finished_at"
    )
    base_vals = (
        ":i, :c, :u, 'stroke-lnm', '0.1.0', :sha, '1.0', '1.0', 'ct-and-lesion', 7,"
        " 'succeeded', '{}'::jsonb, 'user:x', 'service-account', now(), now(), now()"
    )

    def run_sql(extra_cols: str, extra_vals: str) -> str:
        return (
            f"INSERT INTO pipeline_runs ({base_cols}, {extra_cols})"
            f" VALUES ({base_vals}, {extra_vals})"
        )

    def params(**kw):
        return {
            "i": uuid.uuid4(),
            "c": PIPELINE_CASE,
            "u": OWNER_A,
            "sha": "a" * 64,
            "pid": "mni-mask-analysis",
            "pv": "1.0.0",
            "ps": PRESET_SHA,
            **kw,
        }

    cols = "plan_mode, preset_id, preset_version, preset_sha256, targets"
    ok_preset = run_sql(cols, "'preset', :pid, :pv, :ps, CAST(:t AS jsonb)")
    targets = json.dumps({"required": ["a.b"], "if_available": []})
    with pg_engine.begin() as conn:  # a preset run and a free run are accepted
        conn.execute(text(ok_preset), params(t=targets))
        conn.execute(
            text(run_sql("plan_mode, targets", "'free', CAST(:t AS jsonb)")),
            params(t=targets),
        )
    stored = one(pg_engine, "SELECT targets FROM pipeline_runs WHERE plan_mode = 'preset'")
    assert stored["targets"] == {"required": ["a.b"], "if_available": []}

    assert "ck_pipeline_runs_plan_mode" in _violation(
        pg_engine, run_sql("plan_mode", "'custom'"), **params()
    )
    # preset mode needs the preset, and the preset columns only go with preset mode
    assert "ck_pipeline_runs_preset_mode" in _violation(
        pg_engine, run_sql("plan_mode", "'preset'"), **params()
    )
    assert "ck_pipeline_runs_preset_mode" in _violation(
        pg_engine,
        run_sql(cols, "'free', :pid, :pv, :ps, CAST(:t AS jsonb)"),
        **params(t=targets),
    )
    assert "ck_pipeline_runs_preset_mode" in _violation(
        pg_engine,
        run_sql("preset_id, preset_version, preset_sha256", ":pid, :pv, :ps"),
        **params(),
    )
    # the three preset columns are all null or all set
    for values in (":pid, NULL, :ps", ":pid, :pv, NULL", ":pid, NULL, NULL"):
        assert "ck_pipeline_runs_preset_all_or_none" in _violation(
            pg_engine,
            run_sql(
                "plan_mode, preset_id, preset_version, preset_sha256",
                f"'preset', {values}",
            ),
            **params(),
        )
    # formats
    assert "ck_pipeline_runs_preset_id_format" in _violation(
        pg_engine, ok_preset, **params(pid="Not Kebab", t=targets)
    )
    assert "ck_pipeline_runs_preset_version_format" in _violation(
        pg_engine, ok_preset, **params(pv="v1", t=targets)
    )
    assert "ck_pipeline_runs_preset_sha256_format" in _violation(
        pg_engine, ok_preset, **params(ps="C" * 64, t=targets)
    )
    # targets is never NULL
    assert "targets" in _violation(pg_engine, run_sql("targets", "NULL"), **params())

    # run slots: the new values are accepted, unknown ones are not
    run = PIPELINE_RUN
    with pg_engine.begin() as conn:
        _insert_slot(
            conn,
            run,
            "preset-chosen",
            10,
            False,
            "selected",
            chosen_by="preset",
            plan_role="upstream",
        )
        _insert_slot(conn, run, "outside-plan", 11, False, "not_requested")
        _insert_slot(
            conn,
            run,
            "external",
            12,
            True,
            "selected",
            plan_role="if_available",
            upstream=json.dumps({"tool": "x"}),
        )

    def skipped(reason: str, role: str = "NULL", chosen_by: str = "NULL") -> str:
        return (
            "INSERT INTO run_slots (id, pipeline_run_id, slot, position, optional, status,"
            " reason, plan_role, chosen_by) VALUES (gen_random_uuid(), :r, :s, :p, true,"
            f" 'skipped', {reason}, {role}, {chosen_by})"
        )

    assert "ck_run_slots_reason" in _violation(
        pg_engine, skipped("'nope'"), r=run, s="s1", p=20
    )
    assert "ck_run_slots_plan_role" in _violation(
        pg_engine, skipped("'upstream_absent'", "'sidecar'"), r=run, s="s3", p=22
    )
    # not_requested and condition_false rows have no plan role
    assert "ck_run_slots_plan_role_reason" in _violation(
        pg_engine, skipped("'not_requested'", "'target'"), r=run, s="s4", p=23
    )
    assert "ck_run_slots_plan_role_reason" in _violation(
        pg_engine, skipped("'condition_false'", "'if_available'"), r=run, s="s5", p=24
    )
    # chosen_by accepts preset and nothing else new; ck_run_slots_selected still applies
    with pytest.raises(IntegrityError) as err:  # a selected row: only chosen_by can fire
        with pg_engine.begin() as conn:
            _insert_slot(conn, run, "s2", 21, False, "selected", chosen_by="admin")
    assert 'constraint "ck_run_slots_chosen_by"' in str(err.value)
    assert "ck_run_slots_selected" in _violation(
        pg_engine,
        skipped("'not_requested'", "NULL", "'preset'"),
        r=run,
        s="s6",
        p=25,
    )

    # preset settings: same rules as slot_implementation_settings
    def setting(pipeline: str, preset: str, enabled: str, default: str) -> str:
        return (
            "INSERT INTO preset_settings (id, pipeline_id, preset_id, enabled, is_default,"
            f" updated_at) VALUES (gen_random_uuid(), '{pipeline}', '{preset}', {enabled},"
            f" {default}, now())"
        )

    with pg_engine.begin() as conn:
        conn.execute(text(setting("stroke-lnm", "full-lnm", "true", "true")))
        conn.execute(text(setting("stroke-lnm", "ct-only", "true", "false")))
        conn.execute(text(setting("other-pipeline", "full-lnm", "true", "true")))
    assert "uq_preset_settings_default" in _violation(
        pg_engine, setting("stroke-lnm", "qc-only", "true", "true")
    )
    assert "ck_preset_settings_default_enabled" in _violation(
        pg_engine, setting("stroke-lnm", "qc-only", "false", "true")
    )
    assert "uq_preset_settings" in _violation(
        pg_engine, setting("stroke-lnm", "ct-only", "true", "false")
    )
    assert "ck_preset_settings_preset_id_format" in _violation(
        pg_engine, setting("stroke-lnm", "Bad_Id", "true", "false")
    )
    assert "ck_preset_settings_pipeline_id_format" in _violation(
        pg_engine, setting("Bad Id", "ct-only", "true", "false")
    )

    # input files: the declared origin is stored as given
    origin = {"kind": "external-tool", "tool": "some-tool", "version": "1.2"}
    with pg_engine.begin() as conn:
        conn.execute(
            text("UPDATE input_files SET origin = CAST(:o AS jsonb) WHERE case_id = :c"),
            {"o": json.dumps(origin), "c": PIPELINE_CASE},
        )
    stored = one(pg_engine, "SELECT origin FROM input_files WHERE case_id = :c", c=PIPELINE_CASE)
    assert stored["origin"] == origin


def test_0013_downgrade_restores_the_0012_schema_and_data(pg_engine, at_0012):
    columns = {t: _table_columns(pg_engine, t) for t in PLAN_TABLES}
    checks = {t: _check_names(pg_engine, t) for t in PLAN_TABLES}
    before = _snapshot(pg_engine, columns)
    alembic("upgrade", REV_PLANS, STORAGE_ROOT=str(at_0012))
    # preset settings are the only new data a downgrade drops (with a warning)
    with pg_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO preset_settings (id, pipeline_id, preset_id, enabled,"
                " is_default, updated_at) VALUES (gen_random_uuid(), 'stroke-lnm',"
                " 'ct-only', true, false, now())"
            )
        )

    alembic("downgrade", REV_CUTOVER, STORAGE_ROOT=str(at_0012))
    assert version(pg_engine) == REV_CUTOVER
    assert "preset_settings" not in user_tables(pg_engine)
    for table, cols in columns.items():
        assert _table_columns(pg_engine, table) == cols, table
        assert _check_names(pg_engine, table) == checks[table], table
    assert _snapshot(pg_engine, columns) == before
    # the old value lists are back: the 1.2 values are refused again
    insert = (
        "INSERT INTO run_slots (id, pipeline_run_id, slot, position, optional, status,"
        " reason, chosen_by) VALUES (gen_random_uuid(), :r, 'x', 30, true, 'skipped',"
        " :reason, :chosen)"
    )
    assert "ck_run_slots_reason" in _violation(
        pg_engine, insert, r=PIPELINE_RUN, reason="not_requested", chosen=None
    )
    with pytest.raises(IntegrityError) as err:  # a selected row chosen by a preset
        with pg_engine.begin() as conn:
            _insert_slot(
                conn, PIPELINE_RUN, "x", 31, False, "selected", chosen_by="preset"
            )
    assert 'constraint "ck_run_slots_chosen_by"' in str(err.value)
    for column in ("plan_mode", "targets", "preset_id"):
        assert column not in _table_columns(pg_engine, "pipeline_runs")

    # and the upgrade can be applied again, with the same back-fill
    alembic("upgrade", REV_PLANS, STORAGE_ROOT=str(at_0012))
    assert _plan_roles(pg_engine)[(str(PIPELINE_RUN), "lesion-intake")] == "target"


@pytest.mark.parametrize(
    "statement, message",
    [
        (
            "UPDATE pipeline_runs SET plan_mode = 'free' WHERE id = :r",
            "plan mode other than full",
        ),
        (
            "UPDATE run_slots SET status = 'skipped', reason = 'not_requested',"
            " implementation_id = NULL, step_name = NULL, step_version = NULL, image = NULL,"
            " stub = NULL, chosen_by = NULL, effective_parameters = NULL, plan_role = NULL"
            " WHERE slot = 'qc-report' AND pipeline_run_id = :r",
            "reason not_requested",
        ),
        (
            "UPDATE run_slots SET chosen_by = 'preset' WHERE slot = 'lesion-intake'"
            " AND pipeline_run_id = :r",
            "chosen by a preset",
        ),
        (
            "UPDATE run_slots SET upstream = CAST('{\"tool\": \"x\"}' AS jsonb)"
            " WHERE slot = 'qc-report' AND pipeline_run_id = :r",
            "run slots with an upstream block",
        ),
        (
            "UPDATE input_files SET origin = CAST('{\"kind\": \"manual\"}' AS jsonb)"
            " WHERE case_id IN (SELECT case_id FROM pipeline_runs WHERE id = :r)",
            "input files with a declared origin",
        ),
    ],
)
def test_0013_downgrade_refuses_when_plan_values_are_in_use(
    pg_engine, at_0012, statement, message
):
    alembic("upgrade", REV_PLANS, STORAGE_ROOT=str(at_0012))
    with pg_engine.begin() as conn:
        conn.execute(text(statement), {"r": PIPELINE_RUN})
    refused = alembic_run("downgrade", REV_CUTOVER, STORAGE_ROOT=str(at_0012))
    assert refused.returncode != 0
    assert message in refused.stderr
    assert "remove that data deliberately" in refused.stderr
    # nothing was changed: still at 0013, run and slot rows intact
    assert version(pg_engine) == REV_PLANS
    count = one(pg_engine, "SELECT count(*) AS n FROM run_slots WHERE pipeline_run_id = :r", r=PIPELINE_RUN)
    assert count["n"] == 5
    assert "plan_mode" in _table_columns(pg_engine, "pipeline_runs")


def test_0013_on_an_empty_database(pg_engine):
    alembic("upgrade", "head")
    assert "preset_settings" in user_tables(pg_engine)
    alembic("downgrade", REV_CUTOVER)
    assert version(pg_engine) == REV_CUTOVER
    assert "preset_settings" not in user_tables(pg_engine)
    assert "plan_mode" not in _table_columns(pg_engine, "pipeline_runs")
    assert "plan_role" not in _table_columns(pg_engine, "run_slots")
    assert "origin" not in _table_columns(pg_engine, "input_files")
    assert "ck_run_slots_plan_role" not in _check_names(pg_engine, "run_slots")
    alembic("upgrade", "head")
    assert version(pg_engine) == REV_PLANS
    assert "plan_mode" in _table_columns(pg_engine, "pipeline_runs")
    assert "ck_run_slots_plan_role_reason" in _check_names(pg_engine, "run_slots")
