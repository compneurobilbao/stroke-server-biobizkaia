"""legacy cut-over: final reconciliation of studies into the case model (stage 2)

docs/data-model.md 10.1 (stage 2), 10.3 and 10.5. From this revision on the new tables
are authoritative for legacy studies: the legacy endpoints are aliases over them and the
legacy worker task writes them directly; ``studies`` and ``processing_jobs`` are frozen.

Upgrade:

1. Final reconciliation: the mapping of 10.3 (the 0011 back-fill, reused from that
   migration) is re-run as an upsert. Rows that are missing are inserted, rows whose
   mapped columns differ (for example mirror rows of stage 1b that lagged behind) are
   updated, so the new tables reflect the legacy tables exactly. The step attempts,
   artefacts and report of a study are compared as one set and rewritten together when
   they differ, because row-by-row updates could transiently violate the per-slot
   unique indexes. Unmappable data aborts the migration and lists the ids (the same
   rules as 0011, plus rows of the new model that no legacy row explains). The
   reconciliation is idempotent; ``reconcile(..., dry_run=True)`` (also through
   ``python -m app.cli.legacy_reconcile``) reports the counts without writing.
2. ``audit_events``, ``notifications`` and ``share_links`` rows get ``case_id`` from
   ``study_id`` where it is still missing.
3. Legacy artefacts without a size (back-filled from ``studies``, which never stored
   one) get the size of their file under ``STORAGE_ROOT``, so that the quota (inputs
   plus derivatives, section 12 decision 4) counts them like the artefacts the worker
   writes from now on. A missing file leaves the size NULL and is logged. Sizes are not
   part of the 10.3 mapping, so the reconciliation does not compare them.
4. ``share_links.case_id`` becomes NOT NULL and ``share_links.study_id`` nullable.

Downgrade (10.5, row 0012):

(a) cases with a legacy study: the values that changed after the cut-over (status,
    timestamps, ``deleted_at``, ``keep_forever``, error message, review status, current
    PDF and ZIP paths, warnings and output paths) are written back to ``studies``; only
    fields whose forward mapping differs from the current rows are written, so a study
    untouched since the cut-over is restored bit for bit. ``processing_jobs`` are
    upserted from the attempts; attempts created after the cut-over get jobs with
    ``id = step_runs.id`` (and ``step_runs.legacy_job_id`` is set to it).
(b) cases without a study whose runs are all ``legacy`` (uploaded through the alias):
    ``studies`` and ``processing_jobs`` rows are inserted with the mapping in reverse
    (``legacy_attributes.original_filename`` for the file name) and the case is linked
    to its new study.
(c) other cases stay only in the new tables, which still exist.
(d) share links without a study (links of (c) cases) are deleted, the count is logged,
    and ``share_links.study_id`` becomes NOT NULL again (``case_id`` nullable).

Revision ID: 0012_legacy_cutover
Revises: 0011_case_model
Create Date: 2026-11-16
"""

from __future__ import annotations

import importlib.util
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0012_legacy_cutover"
down_revision = "0011_case_model"
branch_labels = None
depends_on = None

log = logging.getLogger("alembic.runtime.migration")

PARENT_TABLES = {
    # table -> key columns; parents first
    "cases": ("id",),
    "input_files": ("id",),
    "pipeline_runs": ("id",),
    "run_inputs": ("pipeline_run_id", "role"),
    "run_slots": ("id",),
}
CHILD_TABLES = ("step_runs", "artifacts", "reports")  # rewritten as one set per run
# Columns the 10.3 mapping cannot know (filled later from the files); not compared.
NOT_COMPARED = {"artifacts": {"size_bytes", "sha256"}}
LINKED_TABLES = ("audit_events", "notifications", "share_links")
LEGACY_TYPES = {
    "technical_report": "legacy_technical_report_pdf",
    "outputs_zip": "legacy_outputs_zip",
}
STUDY_STATUS = {
    "queued": "queued",
    "running": "processing",
    "awaiting_review": "processing",
    "succeeded": "completed",
    "failed": "failed",
    "canceled": "canceled",
}
PATH_ATTRIBUTES = ("output_path", "preproc_output_path", "rendered_png_dir", "bids_path")

_m11_module = None


def _m11():
    """The 0011 migration module: its mapping is the one of docs/data-model.md 10.3."""
    global _m11_module
    if _m11_module is None:
        path = Path(__file__).with_name("0011_case_model.py")
        spec = importlib.util.spec_from_file_location("_migration_0011_case_model", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _m11_module = module
    return _m11_module


def _expanding(sql: str, **params):
    statement = sa.text(sql)
    for name in params:
        statement = statement.bindparams(sa.bindparam(name, expanding=True))
    return statement


def _naive(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


# --- upgrade -----------------------------------------------------------------------------
def upgrade() -> None:
    from app.core.config import get_settings

    conn = op.get_bind()
    root = Path(get_settings().storage_root)
    report = reconcile(conn, root)
    log.info("legacy reconciliation: %s", json.dumps(report, sort_keys=True))
    sizes = fill_legacy_artifact_sizes(conn, root)
    log.info("legacy artefact sizes: %s", json.dumps(sizes, sort_keys=True))
    op.alter_column("share_links", "case_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column("share_links", "study_id", existing_type=sa.Uuid(), nullable=True)


def _load_legacy(conn):
    m = _m11()
    studies = conn.execute(
        sa.text(f"SELECT {m.STUDY_COLUMNS} FROM studies ORDER BY id")
    ).mappings().all()
    jobs_by_study: dict[uuid.UUID, list] = {}
    for job in conn.execute(
        sa.text(
            f"SELECT {m.JOB_COLUMNS} FROM processing_jobs ORDER BY study_id, queued_at, id"
        )
    ).mappings():
        jobs_by_study.setdefault(job["study_id"], []).append(job)
    return studies, jobs_by_study


def _plan(conn, storage_root: Path, studies, jobs_by_study) -> tuple[list, list[str]]:
    """Validate every study as the 0011 back-fill does, plus the rows of the new model
    that the mapping cannot account for. Returns the plan and the errors."""
    m = _m11()
    errors: list[str] = []
    labels = {
        label: case_id
        for label, case_id in conn.execute(sa.text("SELECT subject_label, id FROM cases"))
    }
    seen_labels: dict[str, uuid.UUID] = {}

    def rel(value, what: str, owner) -> str | None:
        try:
            return m._relative(value, storage_root)
        except ValueError:
            errors.append(f"{what} of {owner} is outside the storage root: {value}")
            return None

    plan = []
    for study in studies:
        sid = study["id"]
        jobs = jobs_by_study.get(sid, [])
        label = sid.hex[:12]
        if seen_labels.setdefault(label, sid) != sid:
            errors.append(f"subject label collision between {seen_labels[label]} and {sid}")
        if labels.get(label, sid) != sid:
            errors.append(f"subject label of study {sid} is taken by case {labels[label]}")
        if study["status"] not in m.RUN_STATUS:
            errors.append(f"study {sid} has unknown status {study['status']!r}")
        if study["clinical_review_status"] not in m.REVIEW_STATUSES:
            errors.append(
                f"study {sid} has unknown clinical_review_status"
                f" {study['clinical_review_status']!r}"
            )
        for job in jobs:
            if job["status"] not in m.RUN_STATUS:
                errors.append(f"job {job['id']} has unknown status {job['status']!r}")
        if sum(1 for j in jobs if j["status"] == "completed") > 1:
            errors.append(f"study {sid} has more than one completed job")
        paths = {
            name: rel(study[name], name, f"study {sid}")
            for name in (
                "stored_path",
                "output_path",
                "pdf_path",
                "output_zip_path",
                "bids_path",
                "preproc_output_path",
                "rendered_png_dir",
            )
        }
        if not paths["stored_path"]:
            errors.append(f"study {sid} has an empty stored_path")
        log_paths = {
            j["id"]: rel(j["log_path"], "log_path", f"job {j['id']}") for j in jobs
        }
        plan.append((study, jobs, paths, log_paths))

    study_ids = [study["id"] for study in studies]
    if study_ids:
        for run_id, case_id in conn.execute(
            _expanding(
                "SELECT id, case_id FROM pipeline_runs"
                " WHERE case_id IN :ids AND id <> case_id",
                ids=study_ids,
            ),
            {"ids": study_ids},
        ):
            errors.append(f"run {run_id} of legacy study {case_id} has no legacy job")
        job_ids = {j["id"] for jobs in jobs_by_study.values() for j in jobs}
        for step_id, run_id in conn.execute(
            _expanding(
                "SELECT id, pipeline_run_id FROM step_runs WHERE pipeline_run_id IN :ids",
                ids=study_ids,
            ),
            {"ids": study_ids},
        ):
            if step_id not in job_ids:
                errors.append(f"attempt {step_id} of legacy study {run_id} has no legacy job")
        for link_id, run_id in conn.execute(
            _expanding(
                "SELECT s.id, r.pipeline_run_id FROM share_links s"
                " JOIN reports r ON r.id = s.report_id WHERE r.pipeline_run_id IN :ids",
                ids=study_ids,
            ),
            {"ids": study_ids},
        ):
            errors.append(f"share link {link_id} is bound to a report of study {run_id}")
    return plan, errors


def _key(table: str, row) -> tuple:
    return tuple(row[c] for c in PARENT_TABLES.get(table, ("id",)))


def _existing(conn, table: str, columns: list[str], where: str, ids: list) -> list[dict]:
    if not ids:
        return []
    return [
        dict(r)
        for r in conn.execute(
            _expanding(f"SELECT {', '.join(columns)} FROM {table} WHERE {where} IN :ids", ids=ids),
            {"ids": ids},
        ).mappings()
    ]


def _update(conn, table: str, row: dict, changed: list[str]) -> None:
    m = _m11()
    keys = PARENT_TABLES[table]
    sets = ", ".join(
        f"{c} = CAST(:{c} AS jsonb)" if c in m.JSON_COLUMNS else f"{c} = :{c}"
        for c in changed
    )
    where = " AND ".join(f"{k} = :{k}" for k in keys)
    params = m._jsonable({c: row[c] for c in (*changed, *keys)})
    conn.execute(sa.text(f"UPDATE {table} SET {sets} WHERE {where}"), params)


def reconcile(conn, storage_root: Path, *, dry_run: bool = False) -> dict:
    """Re-run the 10.3 mapping as an upsert; report what is (or would be) written.

    Raises RuntimeError, writing nothing, when the legacy data cannot be mapped.
    Idempotent: a second run reports no insert, update or rewrite.
    """
    m = _m11()
    studies, jobs_by_study = _load_legacy(conn)
    plan, errors = _plan(conn, storage_root, studies, jobs_by_study)
    if errors:
        raise RuntimeError(
            "legacy data cannot be reconciled with the new model; fix it and re-run:\n  "
            + "\n  ".join(errors)
        )
    rows: dict[str, list[dict]] = {t: [] for t in (*PARENT_TABLES, *CHILD_TABLES)}
    for study, jobs, paths, log_paths in plan:
        m._map_study(study, jobs, paths, log_paths, rows)

    report: dict = {"studies": len(studies), "dry_run": dry_run}
    updates: list[tuple[str, dict, list[str]]] = []
    inserts: dict[str, list[dict]] = {}
    for table, keys in PARENT_TABLES.items():
        mapped = rows[table]
        counts = {"insert": 0, "update": 0, "unchanged": 0}
        report[table] = counts
        if not mapped:
            continue
        columns = list(mapped[0])
        where = keys[0]
        current = {
            _key(table, r): r
            for r in _existing(
                conn, table, columns, where, sorted({r[where] for r in mapped})
            )
        }
        for row in mapped:
            existing = current.get(_key(table, row))
            if existing is None:
                inserts.setdefault(table, []).append(row)
                counts["insert"] += 1
                continue
            changed = [c for c in columns if c not in keys and existing[c] != row[c]]
            if changed:
                updates.append((table, row, changed))
                counts["update"] += 1
            else:
                counts["unchanged"] += 1

    # Attempts, artefacts and the report of each legacy run, compared as one set.
    run_ids = [s["id"] for s in studies]
    mapped_children: dict[uuid.UUID, dict[str, dict]] = {rid: {} for rid in run_ids}
    columns_of: dict[str, list[str]] = {}
    for table in CHILD_TABLES:
        for row in rows[table]:
            columns_of.setdefault(
                table, [c for c in row if c not in NOT_COMPARED.get(table, ())]
            )
            mapped_children[row["pipeline_run_id"]][f"{table}/{row['id']}"] = {
                c: v for c, v in row.items() if c in columns_of[table]
            }
    current_children: dict[uuid.UUID, dict[str, dict]] = {rid: {} for rid in run_ids}
    for table in CHILD_TABLES:
        if table not in columns_of:
            columns_of[table] = ["id", "pipeline_run_id"]
        for row in _existing(conn, table, columns_of[table], "pipeline_run_id", run_ids):
            current_children[row["pipeline_run_id"]][f"{table}/{row['id']}"] = row
    rewrite = [
        rid
        for rid in run_ids
        if {
            k: {c: v for c, v in r.items() if c in columns_of[k.split("/")[0]]}
            for k, r in current_children[rid].items()
        }
        != mapped_children[rid]
    ]
    report["attempt_sets"] = {
        "rewrite": len(rewrite),
        "unchanged": len(run_ids) - len(rewrite),
    }

    linked = {}
    for table in LINKED_TABLES:
        linked[table] = conn.execute(
            sa.text(
                f"SELECT count(*) FROM {table} WHERE case_id IS NULL AND study_id IS NOT NULL"
            )
        ).scalar()
    report["linked_to_case"] = linked
    if dry_run:
        return report

    for table in PARENT_TABLES:
        m._insert(conn, table, inserts.get(table, []))
    for table, row, changed in updates:
        _update(conn, table, row, changed)
    if rewrite:
        params = {"ids": rewrite}
        conn.execute(
            _expanding(
                "UPDATE pipeline_runs SET failed_step_run_id = NULL WHERE id IN :ids",
                ids=rewrite,
            ),
            params,
        )
        for table in ("reports", "artifacts", "step_runs"):
            conn.execute(
                _expanding(f"DELETE FROM {table} WHERE pipeline_run_id IN :ids", ids=rewrite),
                params,
            )
        targets = set(rewrite)
        for table in CHILD_TABLES:
            m._insert(
                conn, table, [r for r in rows[table] if r["pipeline_run_id"] in targets]
            )
    for table in LINKED_TABLES:
        conn.execute(
            sa.text(
                f"UPDATE {table} SET case_id = study_id"
                " WHERE case_id IS NULL AND study_id IS NOT NULL"
            )
        )
    return report


def fill_legacy_artifact_sizes(conn, storage_root: Path, *, dry_run: bool = False) -> dict:
    """Size of every legacy artefact that has none, from its file under the storage root.

    A missing file (or a path that leaves the root) leaves the size NULL; the count and
    the first ids are logged. Idempotent: only NULL sizes are touched.
    """
    root = storage_root.resolve()
    filled, missing = 0, []
    for artifact_id, rel in conn.execute(
        sa.text(
            "SELECT id, storage_path FROM artifacts"
            " WHERE origin = 'legacy' AND size_bytes IS NULL ORDER BY id"
        )
    ).all():
        path = (storage_root / rel).resolve()
        inside = path == root or root in path.parents
        if not inside or not path.is_file():
            missing.append(artifact_id)
            continue
        filled += 1
        if not dry_run:
            conn.execute(
                sa.text(
                    "UPDATE artifacts SET size_bytes = :s WHERE id = :i AND size_bytes IS NULL"
                ),
                {"s": path.stat().st_size, "i": artifact_id},
            )
    if missing:
        log.warning(
            "%s legacy artefact(s) have no file under the storage root; size left empty"
            " (first ids: %s)",
            len(missing),
            ", ".join(str(i) for i in missing[:10]),
        )
    return {"filled": filled, "missing_file": len(missing), "dry_run": dry_run}


# --- downgrade ---------------------------------------------------------------------------
_UNMAPPABLE = object()  # a forward value that never equals a stored one


def downgrade() -> None:
    from app.core.config import get_settings

    conn = op.get_bind()
    report = restore_legacy(conn, Path(get_settings().storage_root))
    log.info("legacy restore: %s", json.dumps(report, sort_keys=True))
    deleted = conn.execute(
        sa.text("DELETE FROM share_links WHERE study_id IS NULL")
    ).rowcount
    if deleted:
        log.warning(
            "deleted %s share link(s) of cases created after the cut-over without a"
            " legacy study (docs/data-model.md 10.5 (d))",
            deleted,
        )
    op.alter_column("share_links", "study_id", existing_type=sa.Uuid(), nullable=False)
    op.alter_column("share_links", "case_id", existing_type=sa.Uuid(), nullable=True)


def _forward(study, jobs, storage_root: Path) -> dict[str, list[dict]]:
    """The 10.3 mapping of the frozen legacy rows (what the new tables held at the
    cut-over, as far as the legacy rows can say)."""
    m = _m11()

    def rel(value):
        try:
            return m._relative(value, storage_root)
        except ValueError:
            return _UNMAPPABLE

    paths = {
        name: rel(study[name])
        for name in (
            "stored_path",
            "output_path",
            "pdf_path",
            "output_zip_path",
            "bids_path",
            "preproc_output_path",
            "rendered_png_dir",
        )
    }
    log_paths = {j["id"]: rel(j["log_path"]) for j in jobs}
    rows: dict[str, list[dict]] = {t: [] for t in (*PARENT_TABLES, *CHILD_TABLES)}
    jobs = [
        j if j["status"] in m.RUN_STATUS else {**j, "status": "failed"} for j in jobs
    ]
    status = study["status"] if study["status"] in m.RUN_STATUS else "failed"
    m._map_study({**study, "status": status}, jobs, paths, log_paths, rows)
    return rows


def _absolute(storage_root: Path, rel: str | None) -> str | None:
    return str(storage_root / rel) if rel else None


def _worker(state) -> str | None:
    return state.get("worker_name") if isinstance(state, dict) else None


def _job_values(step: dict, storage_root: Path) -> dict:
    """``processing_jobs`` columns of an attempt (10.3 in reverse)."""
    return {
        "status": STUDY_STATUS[step["status"]],
        "started_at": _naive(step["started_at"]),
        "finished_at": _naive(step["finished_at"]),
        "exit_code": step["exit_code"],
        "cancel_requested_at": _naive(step["cancel_requested_at"]),
        "error_message": step["failure_message"],
        "log_path": _absolute(storage_root, step["log_path"]),
        "celery_task_id": step["orchestrator_task_id"],
        "worker_name": _worker(step["executor_state"]),
    }


# step_runs column -> processing_jobs column
JOB_FIELDS = {
    "status": "status",
    "started_at": "started_at",
    "finished_at": "finished_at",
    "exit_code": "exit_code",
    "cancel_requested_at": "cancel_requested_at",
    "failure_message": "error_message",
    "log_path": "log_path",
    "orchestrator_task_id": "celery_task_id",
    "executor_state": "worker_name",
}


def _current(conn, case_id: uuid.UUID) -> dict:
    """Case, latest legacy run, its attempts, its legacy artefacts and the legacy upload."""
    case = conn.execute(
        sa.text("SELECT * FROM cases WHERE id = :i"), {"i": case_id}
    ).mappings().one()
    run = conn.execute(
        sa.text(
            "SELECT * FROM pipeline_runs WHERE case_id = :i AND pipeline_id = 'legacy'"
            " ORDER BY created_at DESC, id DESC LIMIT 1"
        ),
        {"i": case_id},
    ).mappings().first()
    steps, artefacts = [], {}
    if run is not None:
        steps = conn.execute(
            sa.text(
                "SELECT * FROM step_runs WHERE pipeline_run_id = :r ORDER BY attempt, id"
            ),
            {"r": run["id"]},
        ).mappings().all()
        for row in conn.execute(
            sa.text(
                "SELECT * FROM artifacts WHERE pipeline_run_id = :r"
                " AND artefact_type IN ('legacy_technical_report_pdf', 'legacy_outputs_zip')"
                " ORDER BY created_at, id"
            ),
            {"r": run["id"]},
        ).mappings():
            artefacts[row["port"]] = row
    upload = conn.execute(
        sa.text(
            "SELECT * FROM input_files WHERE case_id = :i AND role = 'legacy_upload'"
        ),
        {"i": case_id},
    ).mappings().first()
    return {"case": case, "run": run, "steps": steps, "artefacts": artefacts, "upload": upload}


def _insert_job(conn, case_id, step: dict, storage_root: Path) -> None:
    values = _job_values(step, storage_root)
    conn.execute(
        sa.text(
            "INSERT INTO processing_jobs (id, study_id, celery_task_id, status, queued_at,"
            " started_at, finished_at, cancel_requested_at, retry_count, worker_name,"
            " exit_code, log_path, error_message) VALUES (:id, :study_id, :celery_task_id,"
            " :status, :queued_at, :started_at, :finished_at, :cancel_requested_at,"
            " :retry_count, :worker_name, :exit_code, :log_path, :error_message)"
        ),
        {
            **values,
            "id": step["id"],
            "study_id": case_id,
            "queued_at": _naive(step["queued_at"]),
            "retry_count": step["attempt"] - 1,
        },
    )
    conn.execute(
        sa.text("UPDATE step_runs SET legacy_job_id = id WHERE id = :i"), {"i": step["id"]}
    )


def _restore_study(conn, study, jobs, storage_root: Path) -> dict:
    """(a): write back what changed since the cut-over. Returns counts."""
    sid = study["id"]
    now = _current(conn, sid)
    case, run = now["case"], now["run"]
    counts = {"studies_updated": 0, "jobs_updated": 0, "jobs_inserted": 0}
    if run is None:
        return counts
    fw = _forward(study, jobs, storage_root)
    fw_case, fw_run = fw["cases"][0], fw["pipeline_runs"][0]
    fw_art = {a["port"]: a for a in fw["artifacts"]}
    attrs = case["legacy_attributes"] or {}
    fw_attrs = fw_case["legacy_attributes"] or {}

    changes: dict = {}
    if fw_run["status"] != run["status"]:
        changes["status"] = STUDY_STATUS[run["status"]]
    for fw_col, col in (
        ("started_at", "processing_started_at"),
        ("finished_at", "processing_finished_at"),
    ):
        if fw_run[fw_col] != run[fw_col]:
            changes[col] = _naive(run[fw_col])
    if fw_case["deleted_at"] != case["deleted_at"]:
        changes["deleted_at"] = _naive(case["deleted_at"])
    if fw_case["keep_forever"] != case["keep_forever"]:
        changes["keep_forever"] = case["keep_forever"]
    if fw_run["failure_message"] != run["failure_message"]:
        changes["error_message"] = run["failure_message"]
    if fw_run["legacy_clinical_review_status"] != run["legacy_clinical_review_status"]:
        changes["clinical_review_status"] = (
            run["legacy_clinical_review_status"] or "technical_only"
        )
    for port, col in (("technical_report", "pdf_path"), ("outputs_zip", "output_zip_path")):
        before = fw_art[port]["storage_path"] if port in fw_art else None
        current = now["artefacts"][port]["storage_path"] if port in now["artefacts"] else None
        if before != current:
            changes[col] = _absolute(storage_root, current)
    if fw_attrs.get("processing_warnings") != attrs.get("processing_warnings"):
        changes["processing_warnings"] = attrs.get("processing_warnings")
    for key in PATH_ATTRIBUTES:
        if fw_attrs.get(key) != attrs.get(key):
            changes[key] = _absolute(storage_root, attrs.get(key))
    if changes:
        changes["updated_at"] = _naive(max(case["updated_at"], run["updated_at"]))
        sets = ", ".join(
            "status = CAST(:status AS studystatus)" if c == "status" else f"{c} = :{c}"
            for c in changes
        )
        conn.execute(
            sa.text(f"UPDATE studies SET {sets} WHERE id = :sid"), {**changes, "sid": sid}
        )
        counts["studies_updated"] = 1

    fw_steps = {row["id"]: row for row in fw["step_runs"]}
    job_ids = {job["id"] for job in jobs}
    for step in now["steps"]:
        job_id = step["legacy_job_id"] or step["id"]
        if job_id not in job_ids:
            _insert_job(conn, sid, step, storage_root)
            counts["jobs_inserted"] += 1
            continue
        before = fw_steps.get(job_id)
        values = _job_values(step, storage_root)
        job_changes = {
            job_col: values[job_col]
            for step_col, job_col in JOB_FIELDS.items()
            if before is None or before[step_col] != step[step_col]
        }
        if job_changes:
            conn.execute(
                sa.text(
                    "UPDATE processing_jobs SET "
                    + ", ".join(f"{c} = :{c}" for c in job_changes)
                    + " WHERE id = :jid"
                ),
                {**job_changes, "jid": job_id},
            )
            counts["jobs_updated"] += 1
    return counts


def _insert_study(conn, case_id: uuid.UUID, storage_root: Path) -> int:
    """(b): a case uploaded through the alias becomes a study again. Returns jobs."""
    now = _current(conn, case_id)
    case, run, upload = now["case"], now["run"], now["upload"]
    attrs = case["legacy_attributes"] or {}
    pdf = now["artefacts"].get("technical_report")
    zip_ = now["artefacts"].get("outputs_zip")
    conn.execute(
        sa.text(
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
            "id": case_id,
            "owner": case["owner_user_id"],
            "fname": attrs.get("original_filename") or "unknown",
            "stored": _absolute(storage_root, upload["storage_path"]) if upload else "",
            "out": _absolute(storage_root, attrs.get("output_path")),
            "pdf": _absolute(storage_root, pdf["storage_path"]) if pdf else None,
            "zip": _absolute(storage_root, zip_["storage_path"]) if zip_ else None,
            "bids_id": attrs.get("bids_subject_id"),
            "backend": attrs.get("processor_backend"),
            "image": attrs.get("container_image"),
            "bids": _absolute(storage_root, attrs.get("bids_path")),
            "preproc": _absolute(storage_root, attrs.get("preproc_output_path")),
            "png": _absolute(storage_root, attrs.get("rendered_png_dir")),
            "warn": attrs.get("processing_warnings"),
            "review": run["legacy_clinical_review_status"] or "technical_only",
            "status": STUDY_STATUS[run["status"]],
            "created": _naive(case["created_at"]),
            "updated": _naive(max(case["updated_at"], run["updated_at"])),
            "started": _naive(run["started_at"]),
            "finished": _naive(run["finished_at"]),
            "deleted": _naive(case["deleted_at"]),
            "keep": case["keep_forever"],
            "error": run["failure_message"],
            "pname": attrs.get("processor_name"),
            "pver": attrs.get("processor_version"),
            "size": upload["size_bytes"] if upload else None,
            "checksum": upload["sha256"] if upload else None,
        },
    )
    conn.execute(
        sa.text("UPDATE cases SET legacy_study_id = id WHERE id = :i"), {"i": case_id}
    )
    for step in now["steps"]:
        _insert_job(conn, case_id, step, storage_root)
    return len(now["steps"])


def restore_legacy(conn, storage_root: Path) -> dict:
    """Steps (a) and (b) of the downgrade, plus the study links of dependent rows."""
    studies, jobs_by_study = _load_legacy(conn)
    report = {
        "studies_updated": 0,
        "jobs_updated": 0,
        "jobs_inserted": 0,
        "studies_inserted": 0,
    }
    for study in studies:
        counts = _restore_study(conn, study, jobs_by_study.get(study["id"], []), storage_root)
        for key, value in counts.items():
            report[key] += value
    alias_cases = conn.execute(
        sa.text(
            "SELECT c.id FROM cases c WHERE c.legacy_study_id IS NULL"
            " AND EXISTS (SELECT 1 FROM pipeline_runs r WHERE r.case_id = c.id)"
            " AND NOT EXISTS (SELECT 1 FROM pipeline_runs r WHERE r.case_id = c.id"
            " AND r.pipeline_id <> 'legacy') ORDER BY c.created_at, c.id"
        )
    ).scalars().all()
    for case_id in alias_cases:
        report["jobs_inserted"] += _insert_study(conn, case_id, storage_root)
        report["studies_inserted"] += 1
    report["cases_kept_without_study"] = conn.execute(
        sa.text("SELECT count(*) FROM cases WHERE legacy_study_id IS NULL")
    ).scalar()
    for table in LINKED_TABLES:
        conn.execute(
            sa.text(
                f"UPDATE {table} SET study_id = case_id WHERE study_id IS NULL"
                " AND case_id IN (SELECT legacy_study_id FROM cases"
                " WHERE legacy_study_id IS NOT NULL)"
            )
        )
    return report
