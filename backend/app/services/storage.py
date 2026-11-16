import hashlib
import json
import shutil
from pathlib import Path
from uuid import UUID

from fastapi import UploadFile


class LocalStudyStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def study_dir(self, study_id: UUID) -> Path:
        return self.root / str(study_id)

    def input_dir(self, study_id: UUID) -> Path:
        return self.study_dir(study_id) / "input"

    def input_original_dir(self, study_id: UUID) -> Path:
        return self.input_dir(study_id) / "original"

    def bids_project_dir(self, study_id: UUID) -> Path:
        return self.study_dir(study_id) / "bids_project"

    def bids_data_dir(self, study_id: UUID) -> Path:
        return self.bids_project_dir(study_id) / "data"

    def runtime_project_dir(self, study_id: UUID) -> Path:
        return self.study_dir(study_id) / "runtime_project"

    def output_dir(self, study_id: UUID) -> Path:
        return self.study_dir(study_id) / "output"

    def preproc_output_dir(self, study_id: UUID) -> Path:
        return self.output_dir(study_id) / "Preproc"

    def logs_dir(self, study_id: UUID) -> Path:
        return self.study_dir(study_id) / "logs"

    def rendered_png_dir(self, study_id: UUID) -> Path:
        return self.output_dir(study_id) / "rendered_png"

    def reports_dir(self, study_id: UUID) -> Path:
        return self.output_dir(study_id) / "reports"

    def technical_report_path(self, study_id: UUID, filename: str = "technical_report.pdf") -> Path:
        return self.reports_dir(study_id) / filename

    def output_zip_path(self, study_id: UUID) -> Path:
        return self.output_dir(study_id) / "outputs.zip"

    def prepare(self, study_id: UUID) -> None:
        for directory in [
            self.input_dir(study_id),
            self.input_original_dir(study_id),
            self.bids_data_dir(study_id),
            self.output_dir(study_id),
            self.rendered_png_dir(study_id),
            self.reports_dir(study_id),
            self.logs_dir(study_id),
        ]:
            directory.mkdir(parents=True, exist_ok=True)

    def safe_path(self, base: Path, filename: str) -> Path:
        target = (base / filename).resolve()
        if base.resolve() not in target.parents and target != base.resolve():
            raise ValueError("File path not allowed")
        return target

    def save_upload(self, study_id: UUID, upload: UploadFile, safe_filename: str) -> tuple[Path, int, str]:
        self.prepare(study_id)
        destination = self.safe_path(self.input_original_dir(study_id), safe_filename)
        hasher = hashlib.sha256()
        size = 0
        with destination.open("wb") as out_file:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                hasher.update(chunk)
                out_file.write(chunk)
        return destination, size, hasher.hexdigest()

    def write_metadata(self, study_id: UUID, metadata: dict) -> None:
        path = self.study_dir(study_id) / "metadata.json"
        path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

    def remove_study(self, study_id: UUID) -> None:
        shutil.rmtree(self.study_dir(study_id), ignore_errors=True)

    # ------------------------------------------------------------------
    # Case store (card F0-17, ADR-0005). Per case, below ``<root>/<case id>``:
    #
    #   rawdata/                              BIDS raw dataset (inputs)
    #   runs/<run id>/derivatives/<step>-<v>/ one BIDS derivative dataset per step
    #   runs/<run id>/provenance.json         run-level RunProvenance
    #   runs/<run id>/checksums.json          SHA-256 manifest of the run tree
    #
    # A back-filled legacy case has the same id as its study, so the directory is
    # shared with the legacy layout above, which is left untouched.
    # ------------------------------------------------------------------

    def case_dir(self, case_id: UUID) -> Path:
        return self.study_dir(case_id)

    def rawdata_dir(self, case_id: UUID) -> Path:
        return self.case_dir(case_id) / "rawdata"

    def run_dir(self, case_id: UUID, run_id: UUID) -> Path:
        return self.case_dir(case_id) / "runs" / str(run_id)

    def derivatives_dir(self, case_id: UUID, run_id: UUID) -> Path:
        return self.run_dir(case_id, run_id) / "derivatives"

    def step_derivatives_dir(
        self, case_id: UUID, run_id: UUID, step_name: str, step_version: str
    ) -> Path:
        return self.derivatives_dir(case_id, run_id) / f"{step_name}-{step_version}"

    def run_provenance_path(self, case_id: UUID, run_id: UUID) -> Path:
        return self.run_dir(case_id, run_id) / "provenance.json"

    def run_checksums_path(self, case_id: UUID, run_id: UUID) -> Path:
        return self.run_dir(case_id, run_id) / "checksums.json"

    def relative_path(self, path: Path) -> str:
        """Path as stored in database columns: relative to the root, '/'-separated."""
        try:
            rel = path.resolve().relative_to(self.root.resolve())
        except ValueError as exc:
            raise ValueError("Path is outside the storage root") from exc
        return rel.as_posix()

    def resolve_relative(self, relative: str) -> Path:
        """Inverse of :meth:`relative_path`; refuses anything leaving the root."""
        parts = relative.split("/")
        if not relative or relative.startswith("/") or "\\" in relative or ".." in parts:
            raise ValueError("File path not allowed")
        return self.safe_path(self.root, relative)

    def _remove_tree(self, target: Path, expected_parent: Path) -> bool:
        """rmtree that refuses symlinks and anything not directly below ``expected_parent``."""
        if target.is_symlink():
            raise ValueError("Refusing to delete a symbolic link")
        if target.parent.resolve() != expected_parent.resolve():
            raise ValueError("Directory path not allowed")
        if not target.exists():
            return False
        shutil.rmtree(target)
        return True

    def remove_run(self, case_id: UUID, run_id: UUID) -> bool:
        """Delete one run's derivatives and provenance; the raw tree stays."""
        runs = self.case_dir(case_id) / "runs"
        return self._remove_tree(self.run_dir(case_id, run_id), runs)

    def remove_case_store(self, case_id: UUID) -> bool:
        """Delete the whole case directory (raw, runs and, for back-filled cases, legacy files)."""
        return self._remove_tree(self.case_dir(case_id), self.root)
