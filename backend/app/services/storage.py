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
