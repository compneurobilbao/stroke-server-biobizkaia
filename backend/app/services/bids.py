"""BIDS case store: raw tree, per-step derivatives, run provenance (card F0-17, ADR-0005).

Two parts live here. ``prepare_single_subject_t1w_bids`` is the legacy single-study
layout and is kept unchanged. Everything below the "Case store" heading implements the
layout of decision D5 and docs/contracts/provenance.md section 5 on top of
``LocalStudyStorage``:

    <case>/rawdata/                                   BIDS raw dataset
    <case>/runs/<run>/derivatives/<step>-<ver>/       BIDS derivative dataset per step
    <case>/runs/<run>/provenance.json                 RunProvenance
    <case>/runs/<run>/checksums.json                  SHA-256 manifest of the run tree

Privacy rule: nothing written here carries a name, an exact age, a date of birth or a
user-chosen file name. The BIDS subject label is the platform-generated
``Case.subject_label``; case metadata is limited to ``sex`` and ``age_band``.

The BIDS standard has no CT datatype and no ``statmap``, ``qc`` or ``report`` suffixes.
Files whose naming the standard does not define are listed in the dataset's
``.bidsignore`` (written by this module), so the validator checks everything it can
and skips only those files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from app.services.storage import LocalStudyStorage


@dataclass(frozen=True)
class BidsStudyPaths:
    bids_project_dir: Path
    bids_data_dir: Path
    t1w_path: Path
    participants_path: Path
    dataset_description_path: Path


def prepare_single_subject_t1w_bids(
    storage: LocalStudyStorage,
    study_id: UUID,
    source_file: Path,
    bids_subject_id: str,
) -> BidsStudyPaths:
    bids_data_dir = storage.bids_data_dir(study_id)
    subject_anat_dir = bids_data_dir / bids_subject_id / "anat"
    subject_anat_dir.mkdir(parents=True, exist_ok=True)

    t1w_path = subject_anat_dir / f"{bids_subject_id}_T1w.nii.gz"
    shutil.copy2(source_file, t1w_path)

    participants_path = bids_data_dir / "participants.tsv"
    participants_path.write_text(f"participant_id\n{bids_subject_id}\n", encoding="utf-8")

    dataset_description_path = bids_data_dir / "dataset_description.json"
    dataset_description_path.write_text(
        json.dumps(
            {
                "Name": "neuroimagen-server-biocruces study",
                "BIDSVersion": "1.11.1",
                "DatasetType": "raw",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    return BidsStudyPaths(
        bids_project_dir=storage.bids_project_dir(study_id),
        bids_data_dir=bids_data_dir,
        t1w_path=t1w_path,
        participants_path=participants_path,
        dataset_description_path=dataset_description_path,
    )


# ----------------------------------------------------------------------------------
# Case store
# ----------------------------------------------------------------------------------

BIDS_VERSION = "1.11.1"
CONTRACT_VERSION = "1.0"
PLATFORM_NAME = "stroke-server"
# BIDS entity order used in stored names (docs/contracts/artefact-types.md section 4).
ENTITY_ORDER = ("space", "res", "label", "desc", "stat")

_SUBJECT_RE = re.compile(r"^[0-9a-f]{12}$")
_SESSION_RE = re.compile(r"^[a-z0-9]{1,16}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9]{1,64}$")
_EXTENSION_RE = re.compile(r"^\.[a-z0-9]+(\.[a-z0-9]+)?$")
_STEP_NAME_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_STEP_VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
_RUN_FIELDS_UPDATABLE = frozenset({"status", "finished_at", "slots", "inputs", "accounting"})

# Naming the BIDS standard (1.11) defines for the files this platform stores.
_RAW_ANAT_SUFFIXES = frozenset({"T1w", "T2w", "FLAIR", "PDw", "T2starw"})
_DERIV_ANAT_SUFFIXES = _RAW_ANAT_SUFFIXES | {"mask", "dseg", "probseg"}
_NIFTI_EXTENSIONS = frozenset({".nii.gz", ".nii"})
# Datatype directories of BIDS 1.11; any other directory (here: ``ct``) is ignored whole.
_BIDS_DATATYPES = frozenset(
    {"anat", "beh", "dwi", "eeg", "emg", "fmap", "func", "ieeg", "meg", "micr",
     "motion", "mrs", "perf", "pet", "phenotype", "nirs"}
)
_CONTAINER_TYPES = {"docker": "docker", "apptainer": "apptainer"}


class ChecksumMismatch(ValueError):
    """A file does not match the SHA-256 it was declared with."""


class RetentionBlocked(PermissionError):
    """Deletion refused because the case is flagged keep-forever."""


@dataclass(frozen=True)
class CaseIdentity:
    """What the case store needs to know about a case; no personal data by construction."""

    case_id: UUID
    subject_label: str
    session_label: str = "acute"
    sex: str | None = None
    age_band: str | None = None

    def __post_init__(self) -> None:
        if not _SUBJECT_RE.match(self.subject_label):
            raise ValueError("subject_label must be 12 lowercase hexadecimal characters")
        if not _SESSION_RE.match(self.session_label):
            raise ValueError("session_label must be 1 to 16 lowercase alphanumerics")

    @classmethod
    def from_case(cls, case: Any) -> CaseIdentity:
        """Build from a ``models.Case`` row (only the fields below are read)."""
        return cls(
            case_id=case.id,
            subject_label=case.subject_label,
            session_label=case.session_label,
            sex=case.sex,
            age_band=case.age_band,
        )

    @property
    def subject(self) -> str:
        return f"sub-{self.subject_label}"

    @property
    def session(self) -> str:
        return f"ses-{self.session_label}"


@dataclass(frozen=True)
class BidsNaming:
    """BIDS fields of an artefact type: ``bids`` block and ``extension`` of the registry."""

    suffix: str
    extension: str
    datatype: str | None = None
    entities: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _LABEL_RE.match(self.suffix):
            raise ValueError(f"invalid BIDS suffix: {self.suffix!r}")
        if not _EXTENSION_RE.match(self.extension):
            raise ValueError(f"invalid file extension: {self.extension!r}")
        if self.datatype is not None and not _LABEL_RE.match(self.datatype):
            raise ValueError(f"invalid BIDS datatype: {self.datatype!r}")
        for key, value in self.entities.items():
            if key not in ENTITY_ORDER:
                raise ValueError(f"entity not allowed: {key!r}")
            if not _LABEL_RE.match(str(value)):
                raise ValueError(f"invalid value for entity {key!r}: {value!r}")

    @classmethod
    def from_type(cls, type_def: Mapping[str, Any]) -> BidsNaming:
        """From one entry of ``contracts/artefact-types.yaml`` ``types``."""
        bids = type_def["bids"]
        return cls(
            suffix=bids["suffix"],
            extension=type_def["extension"],
            datatype=bids.get("datatype"),
            entities={k: str(v) for k, v in (bids.get("entities") or {}).items()},
        )

    def with_desc(self, port: str) -> BidsNaming:
        """Disambiguate two outputs of one type: add the port name as ``desc``."""
        clean = port.replace("_", "")
        current = self.entities.get("desc")
        desc = clean if not current else f"{current}{clean.capitalize()}"
        return BidsNaming(
            self.suffix, self.extension, self.datatype, {**self.entities, "desc": desc}
        )

    def key(self) -> tuple:
        return (
            self.datatype,
            self.suffix,
            self.extension,
            tuple(sorted(self.entities.items())),
        )


@dataclass(frozen=True)
class StoredFile:
    relative_path: str  # relative to the storage root, as stored in database columns
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class StepOutput:
    port: str
    naming: BidsNaming
    source: Path


@dataclass(frozen=True)
class StepStoreResult:
    derivatives_path: str  # relative to the storage root
    outputs: dict[str, StoredFile]
    provenance: StoredFile | None


def bids_file_name(case: CaseIdentity, naming: BidsNaming) -> str:
    parts = [case.subject, case.session]
    parts += [f"{key}-{naming.entities[key]}" for key in ENTITY_ORDER if key in naming.entities]
    return "_".join(parts) + f"_{naming.suffix}{naming.extension}"


def _bids_relative_dir(case: CaseIdentity, naming: BidsNaming) -> Path:
    base = Path(case.subject) / case.session
    return base / naming.datatype if naming.datatype else base


def _is_bids_standard(dataset_type: str, naming: BidsNaming) -> bool:
    if naming.datatype != "anat" or naming.extension not in _NIFTI_EXTENSIONS:
        return False
    suffixes = _RAW_ANAT_SUFFIXES if dataset_type == "raw" else _DERIV_ANAT_SUFFIXES
    return naming.suffix in suffixes


def _sha256_file(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            hasher.update(chunk)
    return hasher.hexdigest(), size


def _atomic_write_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _write_json(target: Path, data: Any) -> None:
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_bytes(target, text.encode("utf-8"))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _copy_verified(source: Path, target: Path, expected_sha256: str | None) -> tuple[str, int]:
    """Copy through a temporary file; the hash is computed on the bytes written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    hasher = hashlib.sha256()
    size = 0
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as src:
            while chunk := src.read(1024 * 1024):
                size += len(chunk)
                hasher.update(chunk)
                out.write(chunk)
        digest = hasher.hexdigest()
        if expected_sha256 is not None and digest != expected_sha256:
            raise ChecksumMismatch("file does not match its declared SHA-256")
        os.replace(tmp_name, target)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return digest, size


def _register_bidsignore(dataset_root: Path, relative: Path | str) -> None:
    path = dataset_root / ".bidsignore"
    entry = Path(relative).as_posix()
    lines = set(path.read_text(encoding="utf-8").splitlines()) if path.exists() else set()
    lines.discard("")
    if entry not in lines:
        lines.add(entry)
        _atomic_write_bytes(path, ("\n".join(sorted(lines)) + "\n").encode("utf-8"))


def _write_derivative_sidecar(target: Path, naming: BidsNaming) -> None:
    """Sidecar of a standard derivative image: ``Resolution`` is required when ``res`` is
    an entity; only technical description, never case data."""
    sidecar: dict[str, Any] = {"Description": f"{naming.suffix} image written by the case store"}
    if "res" in naming.entities:
        res = naming.entities["res"]
        sidecar["Resolution"] = {res: f"Voxel grid index {res} of the artefact type registry"}
    name = target.name[: -len("".join(target.suffixes))] if target.suffixes else target.name
    _write_json(target.with_name(name + ".json"), sidecar)


def _store_dataset_file(
    storage: LocalStudyStorage,
    dataset_root: Path,
    dataset_type: str,
    case: CaseIdentity,
    naming: BidsNaming,
    source: Path,
    expected_sha256: str | None,
) -> StoredFile:
    target = dataset_root / _bids_relative_dir(case, naming) / bids_file_name(case, naming)
    sha256, size = _copy_verified(source, target, expected_sha256)
    if naming.datatype is not None and naming.datatype not in _BIDS_DATATYPES:
        # The validator reports an unknown datatype directory itself, so list the directory.
        _register_bidsignore(dataset_root, target.parent.relative_to(dataset_root).as_posix() + "/")
    elif not _is_bids_standard(dataset_type, naming):
        _register_bidsignore(dataset_root, target.relative_to(dataset_root))
    elif dataset_type == "derivative":
        _write_derivative_sidecar(target, naming)
    return StoredFile(storage.relative_path(target), sha256, size)


# --- raw dataset ----------------------------------------------------------------


def ensure_raw_dataset(
    storage: LocalStudyStorage, case: CaseIdentity, *, platform_version: str | None = None
) -> Path:
    """Create or refresh the case's raw BIDS dataset skeleton; returns its root."""
    root = storage.rawdata_dir(case.case_id)
    generated_by: dict[str, str] = {"Name": PLATFORM_NAME}
    if platform_version:
        generated_by["Version"] = platform_version
    _write_json(
        root / "dataset_description.json",
        {
            "Name": "Raw inputs of one stroke-server case",
            "BIDSVersion": BIDS_VERSION,
            "DatasetType": "raw",
            "GeneratedBy": [generated_by],
        },
    )
    _atomic_write_bytes(
        root / "README",
        b"Raw inputs of one case. Synthetic or de-identified imaging only;\n"
        b"research use only, not for clinical decisions.\n",
    )
    sex = case.sex or "n/a"
    age_band = case.age_band or "n/a"
    _atomic_write_bytes(
        root / "participants.tsv",
        f"participant_id\tsex\tage_band\n{case.subject}\t{sex}\t{age_band}\n".encode(),
    )
    _write_json(
        root / "participants.json",
        {
            "participant_id": {"Description": "Platform-generated case label"},
            "sex": {
                "Description": "Sex of the participant as recorded for the case",
                "Levels": {"female": "Female", "male": "Male"},
            },
            "age_band": {
                "Description": "Age band in whole years, lower and upper bound. "
                "Exact ages are never stored."
            },
        },
    )
    # The sessions file keeps the subject directory visible to the validator even when
    # every image is listed in .bidsignore (the standard has no CT datatype).
    _atomic_write_bytes(
        root / case.subject / f"{case.subject}_sessions.tsv",
        f"session_id\n{case.session}\n".encode(),
    )
    if not (root / ".bidsignore").exists():
        _atomic_write_bytes(root / ".bidsignore", b"")
    return root


def store_raw_input(
    storage: LocalStudyStorage,
    case: CaseIdentity,
    naming: BidsNaming,
    source: Path,
    *,
    expected_sha256: str | None = None,
    platform_version: str | None = None,
) -> StoredFile:
    """Copy one input file into the raw tree under its BIDS name and return its record."""
    root = ensure_raw_dataset(storage, case, platform_version=platform_version)
    return _store_dataset_file(storage, root, "raw", case, naming, source, expected_sha256)


# --- derivative datasets --------------------------------------------------------


def ensure_derivative_dataset(
    storage: LocalStudyStorage,
    case: CaseIdentity,
    run_id: UUID,
    step_name: str,
    step_version: str,
    *,
    image: str | None = None,
    runtime: str | None = None,
) -> Path:
    """Create the derivative dataset of one step in a run; ``GeneratedBy`` names the step
    and, when known, the image reference with its digest."""
    if not _STEP_NAME_RE.match(step_name) or not _STEP_VERSION_RE.match(step_version):
        raise ValueError("invalid step name or version")
    root = storage.step_derivatives_dir(case.case_id, run_id, step_name, step_version)
    generated_by: dict[str, Any] = {"Name": step_name, "Version": step_version}
    if image:
        generated_by["Container"] = {
            "Type": _CONTAINER_TYPES.get(runtime or "docker", "docker"),
            "Tag": image,
        }
    _write_json(
        root / "dataset_description.json",
        {
            "Name": f"{step_name} {step_version} derivatives",
            "BIDSVersion": BIDS_VERSION,
            "DatasetType": "derivative",
            "GeneratedBy": [generated_by],
        },
    )
    _atomic_write_bytes(
        root / "README",
        f"Outputs of step {step_name} {step_version} for one run. Research use only.\n".encode(),
    )
    if not (root / ".bidsignore").exists():
        _atomic_write_bytes(root / ".bidsignore", b"")
    # The step's own provenance record is kept verbatim at the dataset root.
    _register_bidsignore(root, "provenance.json")
    return root


# --- run provenance -------------------------------------------------------------


def init_run_provenance(
    storage: LocalStudyStorage, case_id: UUID, run_id: UUID, header: Mapping[str, Any]
) -> dict[str, Any]:
    """Write the initial ``RunProvenance`` (status ``running`` unless ``header`` says
    otherwise, no steps yet). ``header`` carries ``pipeline``, ``platform``, ``created_at``,
    ``seed``, ``input_set``, ``inputs``, ``slots`` and optionally ``accounting``."""
    record: dict[str, Any] = {
        "contract_version": CONTRACT_VERSION,
        "kind": "RunProvenance",
        "run_id": str(run_id),
        "case_id": str(case_id),
        "status": "running",
        **header,
        "steps": [],
    }
    _write_json(storage.run_provenance_path(case_id, run_id), record)
    write_run_checksums(storage, case_id, run_id)
    return record


def load_run_provenance(storage: LocalStudyStorage, case_id: UUID, run_id: UUID) -> dict[str, Any]:
    return _read_json(storage.run_provenance_path(case_id, run_id))


def merge_step_record(run_record: dict[str, Any], step_entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``run_record`` with ``step_entry`` added, replacing an entry of the same
    slot and attempt. The step's own ``provenance`` is carried verbatim."""
    attempt = step_entry["execution"]["attempt"]
    steps = [
        s
        for s in run_record["steps"]
        if not (s["slot"] == step_entry["slot"] and s["execution"]["attempt"] == attempt)
    ]
    steps.append(dict(step_entry))
    return {**run_record, "steps": steps}


def update_run_provenance(
    storage: LocalStudyStorage, case_id: UUID, run_id: UUID, **fields: Any
) -> dict[str, Any]:
    """Update run-level fields (status, finished_at, slots, inputs, accounting)."""
    unknown = set(fields) - _RUN_FIELDS_UPDATABLE
    if unknown:
        raise ValueError(f"fields not updatable: {sorted(unknown)}")
    record = {**load_run_provenance(storage, case_id, run_id), **fields}
    _write_json(storage.run_provenance_path(case_id, run_id), record)
    write_run_checksums(storage, case_id, run_id)
    return record


def write_run_checksums(storage: LocalStudyStorage, case_id: UUID, run_id: UUID) -> dict[str, Any]:
    """(Re)write ``checksums.json``: SHA-256 and size of every file below the run
    directory except the manifest itself. Keys are paths relative to the run directory."""
    run_dir = storage.run_dir(case_id, run_id)
    manifest_path = storage.run_checksums_path(case_id, run_id)
    files: dict[str, dict[str, Any]] = {}
    for path in sorted(p for p in run_dir.rglob("*") if p.is_file() and p != manifest_path):
        if path.name.startswith(".tmp-"):
            continue
        sha256, size = _sha256_file(path)
        files[path.relative_to(run_dir).as_posix()] = {"sha256": sha256, "size_bytes": size}
    manifest = {"algorithm": "sha256", "files": files}
    _write_json(manifest_path, manifest)
    return manifest


def verify_run_tree(storage: LocalStudyStorage, case_id: UUID, run_id: UUID) -> list[str]:
    """Compare the run directory with ``checksums.json``; returns problems (empty if intact)."""
    run_dir = storage.run_dir(case_id, run_id)
    manifest = _read_json(storage.run_checksums_path(case_id, run_id))
    problems: list[str] = []
    for rel, expected in manifest["files"].items():
        path = run_dir / rel
        if not path.is_file():
            problems.append(f"missing: {rel}")
        elif _sha256_file(path)[0] != expected["sha256"]:
            problems.append(f"modified: {rel}")
    return problems


def store_step_result(
    storage: LocalStudyStorage,
    case: CaseIdentity,
    run_id: UUID,
    *,
    slot: str,
    step_name: str,
    step_version: str,
    execution: Mapping[str, Any],
    outputs: Iterable[StepOutput] = (),
    provenance_path: Path | None = None,
    error: Mapping[str, Any] | None = None,
    image: str | None = None,
) -> StepStoreResult:
    """Store one step attempt in the run's derivatives and merge it into the run record.

    Every output is checked against the SHA-256 in the step's ``provenance.json`` (an
    output the record does not declare is refused). The step's file is copied byte for
    byte; the run record embeds the same content. ``init_run_provenance`` must have been
    called for the run.
    """
    outputs = list(outputs)
    step_prov: dict[str, Any] | None = None
    declared: dict[str, str] = {}
    if provenance_path is not None:
        step_prov = _read_json(provenance_path)
        if step_prov["step"] != {"name": step_name, "version": step_version}:
            raise ValueError("provenance step does not match the executed step")
        declared = {o["port"]: o["sha256"] for o in step_prov["outputs"]}
        for out in outputs:
            if out.port not in declared:
                raise ChecksumMismatch(f"output {out.port!r} is not declared in provenance")

    # Same artefact type on several ports of one step: keep names unique.
    counts: dict[tuple, int] = {}
    for out in outputs:
        counts[out.naming.key()] = counts.get(out.naming.key(), 0) + 1

    root = ensure_derivative_dataset(
        storage,
        case,
        run_id,
        step_name,
        step_version,
        image=image,
        runtime=execution.get("runtime"),
    )
    stored: dict[str, StoredFile] = {}
    for out in outputs:
        naming = out.naming.with_desc(out.port) if counts[out.naming.key()] > 1 else out.naming
        stored[out.port] = _store_dataset_file(
            storage, root, "derivative", case, naming, out.source, declared.get(out.port)
        )

    prov_file: StoredFile | None = None
    if provenance_path is not None:
        target = root / "provenance.json"
        sha256, size = _copy_verified(provenance_path, target, None)
        prov_file = StoredFile(storage.relative_path(target), sha256, size)

    record = merge_step_record(
        load_run_provenance(storage, case.case_id, run_id),
        {
            "slot": slot,
            "execution": dict(execution),
            "provenance": step_prov,
            "error": dict(error) if error is not None else None,
        },
    )
    _write_json(storage.run_provenance_path(case.case_id, run_id), record)
    write_run_checksums(storage, case.case_id, run_id)
    return StepStoreResult(storage.relative_path(root), stored, prov_file)


# --- deletion -------------------------------------------------------------------


def delete_run_files(storage: LocalStudyStorage, case_id: UUID, run_id: UUID) -> bool:
    """Delete one run's derivatives and provenance; the raw inputs and other runs stay."""
    return storage.remove_run(case_id, run_id)


def delete_case_files(
    storage: LocalStudyStorage, case_id: UUID, *, keep_forever: bool = False
) -> bool:
    """Delete everything stored for a case, unless it is flagged keep-forever.

    Callers apply the retention rules first (age, terminal status, ``deleted_at``; see
    ``services/retention.py``) and pass the case's flag; this function is the last guard
    and the only place that removes a case directory.
    """
    if keep_forever:
        raise RetentionBlocked("case is flagged keep-forever")
    return storage.remove_case_store(case_id)
