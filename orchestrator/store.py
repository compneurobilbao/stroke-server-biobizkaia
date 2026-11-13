"""The orchestrator's access to the case store (cards F0-15 and F0-17).

Collected outputs, step provenance and the run-level ``RunProvenance`` go through
the F0-17 case store (``app.services.bids``, ``app.services.storage``), used
read-only from here:

    <case>/runs/<run>/derivatives/<step>-<ver>/   BIDS derivative dataset per step
    <case>/runs/<run>/provenance.json             RunProvenance (pipeline_runs.provenance_path)
    <case>/runs/<run>/checksums.json              SHA-256 manifest of the run tree

Executor work directories and platform-generated inputs are kept *outside* the
run tree, so that ``checksums.json`` covers only results:

    <case>/work/<run>/<slot>/attempt-<n>/   executor work directory (in, out, tmp,
                                            ref, logs); everything except
                                            logs/step.log (StepRun.log_path) is
                                            removed after collection
    <case>/work/<run>/staging/<step run>/   run_provenance.json staged for a step

The database stores only paths relative to the storage root.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import UUID

from app.services.storage import LocalStudyStorage

_CHUNK = 1 << 20


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


class RunStore:
    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.storage = LocalStudyStorage(self.root)

    def work_root(self, case_id: UUID, run_id: UUID) -> Path:
        return self.storage.case_dir(case_id) / "work" / str(run_id)

    def work_dir(self, case_id: UUID, run_id: UUID, slot: str, attempt: int) -> Path:
        return self.work_root(case_id, run_id) / slot / f"attempt-{attempt}"

    def staging_dir(self, case_id: UUID, run_id: UUID, step_run_id: UUID) -> Path:
        return self.work_root(case_id, run_id) / "staging" / str(step_run_id)

    def absolute(self, relative: str) -> Path:
        return self.storage.resolve_relative(relative)

    def relative(self, path: Path) -> str | None:
        """Path relative to the storage root, or None if it lies outside it."""
        try:
            return self.storage.relative_path(Path(path))
        except ValueError:
            return None
