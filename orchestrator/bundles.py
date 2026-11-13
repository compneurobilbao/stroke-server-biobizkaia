"""Reference bundle registry (card F0-18; contract: docs/contracts/bundles.md).

A reference bundle is an immutable directory ``<root>/<id>/<version>/`` that holds
``bundle.yaml`` and the files it lists. This module registers, verifies, lists and
resolves bundles. It has no database, Celery or web imports at module level.

Public API
----------
``BundleRegistry(root, store=None)``
    ``root`` is the bundle root (deployment configuration). ``store`` keeps the
    registry rows and defaults to ``JsonFileStore(root / ".registry.json")``;
    ``SqlAlchemyStore(session)`` writes the ``reference_bundles`` table instead.
    Any object with ``list()``, ``get(bundle_id, version)`` and ``save(record)``
    (see ``BundleStore``) works.

``registry.register(source_dir, registered_by=None) -> BundleRecord``
    Fully verifies ``source_dir`` (schema, licence, digest formula, every file's
    size and SHA-256, no unlisted files), copies it into the root unless it is
    already there, and records it. Raises ``BundleVerificationError`` on failure and
    ``BundleError`` if the id and version exist with different content (versions
    are immutable).
``registry.verify(bundle_id, version, full=True) -> BundleRecord``
    Re-hashes everything and stores the outcome (``verified`` or ``failed``). A
    failed bundle is never resolved. ``full=False`` is the cheap pre-mount check
    (manifest hash, listed files exist with the listed size).
``registry.verify_all(full=True) -> list[BundleRecord]``
``registry.list(bundle_type=None, status=None) -> list[BundleRecord]``
``registry.withdraw(bundle_id, version) -> BundleRecord``
    A withdrawn bundle is not resolved for new runs.
``registry.resolve(step_manifest, selection=None, full=False) -> dict[str, ResolvedBundle]``
    ``step_manifest`` is a parsed ``step.yaml`` (or any mapping with a ``bundles``
    list of requirements ``{name, type, id?, version?, required?}``).
    Returns ``{requirement name: ResolvedBundle}``, each with the verified host
    path (``host_path``), the container mount point ``/ref/<name>`` (read-only),
    ``bundle_id``, ``version`` and ``content_digest`` (what the step must report
    in its provenance). ``selection`` optionally pins ``{name: (id, version)}``;
    otherwise the highest active, verified version matching type, id and version
    range is used. Optional requirements that nothing satisfies are omitted. Each
    chosen bundle gets the cheap pre-mount check (``full=True`` re-hashes, as
    required after a copy to another host). Raises ``BundleNotFoundError`` when a
    required bundle is missing and ``BundleVerificationError`` when one fails
    verification; both subclass ``BundleError`` and name every problem found.
``mount_specs(resolved) -> list[tuple[str, str, str]]``
    ``(host_path, container_path, "ro")`` triples for an executor.

Command line: ``python -m orchestrator.bundles`` (or ``scripts/bundles/registry.py``).
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

import yaml

MANIFEST = "bundle.yaml"
MOUNT_PREFIX = "/ref"
_CHUNK = 1 << 20


class BundleError(Exception):
    """Base class of registry errors."""


class BundleNotFoundError(BundleError):
    """A required bundle is not registered (or withdrawn, or no version fits)."""


class BundleVerificationError(BundleError):
    """A bundle fails verification (tampered, incomplete, invalid manifest)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _contracts():
    try:
        from contracts import validate
    except ImportError as exc:  # pragma: no cover - deployment problem
        raise BundleError(
            "the contracts package is not importable (repository root must be on "
            "PYTHONPATH)"
        ) from exc
    return validate


# --------------------------------------------------------------------------- records


@dataclass
class BundleRecord:
    """One registered bundle version; mirrors the ``reference_bundles`` columns."""

    bundle_id: str
    version: str
    bundle_type: str
    title: str
    content_digest: str
    manifest_sha256: str
    synthetic: bool
    licence_spdx: str
    redistributable: bool
    storage_path: str  # relative to the bundle root: <id>/<version>
    citation: str | None = None
    metadata: dict = field(default_factory=dict)
    status: str = "active"  # active | withdrawn
    verification_status: str = "verified"  # verified | failed
    registered_at: str = field(default_factory=_now)
    registered_by_user_id: str | None = None
    last_full_verification_at: str = field(default_factory=_now)
    withdrawn_at: str | None = None
    row_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BundleRecord":
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


@dataclass(frozen=True)
class ResolvedBundle:
    """A verified bundle ready to be mounted for one step requirement."""

    name: str
    bundle_id: str
    version: str
    content_digest: str
    host_path: Path
    mount_path: str  # /ref/<name>
    read_only: bool = True


def mount_specs(resolved: Mapping[str, ResolvedBundle]) -> list[tuple[str, str, str]]:
    return [
        (str(r.host_path), r.mount_path, "ro" if r.read_only else "rw")
        for r in resolved.values()
    ]


# --------------------------------------------------------------------------- stores


class BundleStore(Protocol):
    def list(self) -> list[BundleRecord]: ...

    def get(self, bundle_id: str, version: str) -> BundleRecord | None: ...

    def save(self, record: BundleRecord) -> None: ...


class JsonFileStore:
    """Registry rows in a JSON file (default for CLI use and tests)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def _load(self) -> list[BundleRecord]:
        if not self.path.exists():
            return []
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return [BundleRecord.from_dict(d) for d in data.get("bundles", [])]

    def list(self) -> list[BundleRecord]:
        return self._load()

    def get(self, bundle_id: str, version: str) -> BundleRecord | None:
        for rec in self._load():
            if rec.bundle_id == bundle_id and rec.version == version:
                return rec
        return None

    def save(self, record: BundleRecord) -> None:
        records = [
            r
            for r in self._load()
            if (r.bundle_id, r.version) != (record.bundle_id, record.version)
        ]
        records.append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({"bundles": [r.to_dict() for r in records]}, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)


class SqlAlchemyStore:
    """Registry rows in the ``reference_bundles`` table (imports the models lazily)."""

    def __init__(self, session):
        self.session = session

    @staticmethod
    def _model():
        from app.models.reference_bundle import ReferenceBundle

        return ReferenceBundle

    def _to_record(self, row) -> BundleRecord:
        def iso(v):
            return v.isoformat() if v else None

        return BundleRecord(
            bundle_id=row.bundle_id,
            version=row.version,
            bundle_type=row.bundle_type,
            title=row.title,
            content_digest=row.content_digest,
            manifest_sha256=row.manifest_sha256,
            synthetic=row.synthetic,
            licence_spdx=row.licence_spdx,
            redistributable=row.redistributable,
            storage_path=row.storage_path,
            citation=row.citation,
            metadata=dict(row.metadata_ or {}),
            status=row.status,
            verification_status=row.verification_status,
            registered_at=iso(row.registered_at),
            registered_by_user_id=str(row.registered_by_user_id)
            if row.registered_by_user_id
            else None,
            last_full_verification_at=iso(row.last_full_verification_at),
            withdrawn_at=iso(row.withdrawn_at),
            row_id=str(row.id),
        )

    def list(self) -> list[BundleRecord]:
        model = self._model()
        return [self._to_record(r) for r in self.session.query(model).all()]

    def _row(self, bundle_id: str, version: str):
        model = self._model()
        return (
            self.session.query(model)
            .filter_by(bundle_id=bundle_id, version=version)
            .one_or_none()
        )

    def get(self, bundle_id: str, version: str) -> BundleRecord | None:
        row = self._row(bundle_id, version)
        return self._to_record(row) if row else None

    def save(self, record: BundleRecord) -> None:
        def ts(v):
            return datetime.fromisoformat(v) if v else None

        row = self._row(record.bundle_id, record.version)
        if row is None:
            row = self._model()(id=uuid.UUID(record.row_id))
            self.session.add(row)
        row.bundle_id = record.bundle_id
        row.version = record.version
        row.bundle_type = record.bundle_type
        row.title = record.title[:255]
        row.content_digest = record.content_digest
        row.manifest_sha256 = record.manifest_sha256
        row.synthetic = record.synthetic
        row.licence_spdx = record.licence_spdx
        row.redistributable = record.redistributable
        row.citation = record.citation
        row.metadata_ = dict(record.metadata)
        row.storage_path = record.storage_path
        row.status = record.status
        row.verification_status = record.verification_status
        row.registered_at = ts(record.registered_at)
        row.registered_by_user_id = (
            uuid.UUID(record.registered_by_user_id)
            if record.registered_by_user_id
            else None
        )
        row.last_full_verification_at = ts(record.last_full_verification_at)
        row.withdrawn_at = ts(record.withdrawn_at)
        self.session.commit()


# ------------------------------------------------------------------------ verification


def _load_manifest(directory: Path) -> tuple[dict | None, str | None, list[str]]:
    """Return (manifest, manifest sha256, errors) for a bundle directory."""
    path = directory / MANIFEST
    if not path.is_file():
        return None, None, [f"{MANIFEST} not found in {directory}"]
    raw = path.read_bytes()
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        return None, None, [f"{MANIFEST} cannot be parsed: {exc}"]
    sha = hashlib.sha256(raw).hexdigest()
    if not isinstance(data, dict):
        return None, sha, [f"{MANIFEST} is not a mapping"]
    contracts = _contracts()
    errs = contracts.schema_errors(data, "bundle")
    if errs:
        return None, sha, [f"{MANIFEST}: {e}" for e in errs]
    errs = contracts.check_bundle(data)
    if errs:
        return None, sha, [f"{MANIFEST}: {e}" for e in errs]
    return data, sha, []


def _listed_file(directory: Path, rel: str) -> Path:
    path = directory / rel
    resolved = path.resolve()
    if directory.resolve() not in resolved.parents:
        raise BundleVerificationError(f"listed path escapes the bundle: {rel}")
    return path


def verify_directory(directory: Path, full: bool = True) -> tuple[dict, str]:
    """Verify a bundle directory against its own ``bundle.yaml``.

    Returns ``(manifest, manifest_sha256)`` or raises ``BundleVerificationError``
    listing every problem. ``full=False`` skips hashing (size check only).
    """
    directory = Path(directory)
    manifest, sha, errors = _load_manifest(directory)
    if manifest is None:
        raise BundleVerificationError("; ".join(errors))
    listed = {f["path"]: f for f in manifest["files"]}
    for rel, entry in sorted(listed.items()):
        try:
            path = _listed_file(directory, rel)
        except BundleVerificationError as exc:
            errors.append(str(exc))
            continue
        if path.is_symlink() or not path.is_file():
            errors.append(f"{rel}: missing or not a regular file")
            continue
        if path.stat().st_size != entry["size_bytes"]:
            errors.append(
                f"{rel}: size {path.stat().st_size} != listed {entry['size_bytes']}"
            )
            continue
        if full and _sha256_file(path) != entry["sha256"]:
            errors.append(f"{rel}: SHA-256 mismatch")
    if full:
        for path in sorted(directory.rglob("*")):
            if path.is_dir():
                continue
            rel = path.relative_to(directory).as_posix()
            if rel != MANIFEST and rel not in listed:
                errors.append(f"{rel}: file is not listed in {MANIFEST}")
    if errors:
        raise BundleVerificationError(
            f"{manifest['id']} {manifest['version']}: " + "; ".join(errors)
        )
    return manifest, sha


# ------------------------------------------------------------------------- the registry


class BundleRegistry:
    def __init__(self, root: Path | str, store: BundleStore | None = None):
        self.root = Path(root)
        self.store: BundleStore = store or JsonFileStore(self.root / ".registry.json")

    def path_of(self, record: BundleRecord) -> Path:
        return self.root / record.storage_path

    # -- register / verify / list

    def register(
        self, source_dir: Path | str, registered_by: str | None = None
    ) -> BundleRecord:
        source = Path(source_dir)
        manifest, sha = verify_directory(source, full=True)
        bundle_id, version = manifest["id"], manifest["version"]
        existing = self.store.get(bundle_id, version)
        if existing is not None:
            if (
                existing.manifest_sha256 == sha
                and existing.content_digest == manifest["content_digest"]
            ):
                return existing
            raise BundleError(
                f"{bundle_id} {version} is already registered with different content; "
                "bundle versions are immutable, publish a new version"
            )
        rel = f"{bundle_id}/{version}"
        target = self.root / rel
        if source.resolve() != target.resolve():
            if target.exists():
                raise BundleError(f"{target} exists but is not registered")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=".incoming-", dir=target.parent))
            try:
                staged = tmp / "bundle"
                shutil.copytree(source, staged)
                verify_directory(staged, full=True)  # the copy must be intact too
                staged.rename(target)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
        lic = manifest["licence"]
        record = BundleRecord(
            bundle_id=bundle_id,
            version=version,
            bundle_type=manifest["type"],
            title=manifest["title"],
            content_digest=manifest["content_digest"],
            manifest_sha256=sha,
            synthetic=bool(manifest.get("synthetic", False)),
            licence_spdx=lic["spdx"],
            redistributable=lic["redistributable"],
            citation=manifest.get("citation"),
            metadata=dict(manifest.get("metadata") or {}),
            storage_path=rel,
            registered_by_user_id=registered_by,
        )
        self.store.save(record)
        return record

    def _record(self, bundle_id: str, version: str) -> BundleRecord:
        rec = self.store.get(bundle_id, version)
        if rec is None:
            raise BundleNotFoundError(f"{bundle_id} {version} is not registered")
        return rec

    def _check(self, record: BundleRecord, full: bool) -> None:
        """Raise BundleVerificationError unless the stored bundle is intact."""
        directory = self.path_of(record)
        manifest, sha = verify_directory(directory, full=full)
        if sha != record.manifest_sha256:
            raise BundleVerificationError(
                f"{record.bundle_id} {record.version}: {MANIFEST} changed since registration"
            )
        if manifest["content_digest"] != record.content_digest:
            raise BundleVerificationError(
                f"{record.bundle_id} {record.version}: content digest changed"
            )

    def verify(self, bundle_id: str, version: str, full: bool = True) -> BundleRecord:
        """Verify a registered bundle and store the outcome; re-raises on failure."""
        record = self._record(bundle_id, version)
        try:
            self._check(record, full)
        except BundleVerificationError:
            record.verification_status = "failed"
            self.store.save(record)
            raise
        record.verification_status = "verified"
        if full:
            record.last_full_verification_at = _now()
        self.store.save(record)
        return record

    def verify_all(self, full: bool = True) -> list[tuple[BundleRecord, str | None]]:
        """Verify every registered bundle; returns (record, error message or None)."""
        out = []
        for rec in sorted(self.store.list(), key=lambda r: (r.bundle_id, r.version)):
            try:
                out.append((self.verify(rec.bundle_id, rec.version, full), None))
            except BundleVerificationError as exc:
                out.append((self.store.get(rec.bundle_id, rec.version), str(exc)))
        return out

    def list(
        self, bundle_type: str | None = None, status: str | None = None
    ) -> list[BundleRecord]:
        recs = [
            r
            for r in self.store.list()
            if (bundle_type is None or r.bundle_type == bundle_type)
            and (status is None or r.status == status)
        ]
        return sorted(recs, key=lambda r: (r.bundle_id, _version_key(r.version)))

    def withdraw(self, bundle_id: str, version: str) -> BundleRecord:
        record = self._record(bundle_id, version)
        record.status = "withdrawn"
        record.withdrawn_at = _now()
        self.store.save(record)
        return record

    # -- resolution

    def resolve(
        self,
        step_manifest: Mapping[str, Any],
        selection: Mapping[str, tuple[str, str]] | None = None,
        full: bool = False,
    ) -> dict[str, ResolvedBundle]:
        selection = dict(selection or {})
        contracts = _contracts()
        missing: list[str] = []
        failed: list[str] = []
        resolved: dict[str, ResolvedBundle] = {}
        for req in step_manifest.get("bundles") or []:
            name = req["name"]
            required = req.get("required", True)
            record = self._select(req, selection.get(name), contracts.satisfies)
            if record is None:
                if required:
                    missing.append(
                        f"{name} (type {req['type']}): no suitable active bundle"
                    )
                continue
            if record.verification_status != "verified":
                failed.append(
                    f"{name}: {record.bundle_id} {record.version} failed verification"
                )
                continue
            try:
                self._check(record, full)
            except BundleVerificationError as exc:
                record.verification_status = "failed"
                self.store.save(record)
                failed.append(f"{name}: {exc}")
                continue
            resolved[name] = ResolvedBundle(
                name=name,
                bundle_id=record.bundle_id,
                version=record.version,
                content_digest=record.content_digest,
                host_path=self.path_of(record),
                mount_path=f"{MOUNT_PREFIX}/{name}",
            )
        unknown = set(selection) - {
            r["name"] for r in step_manifest.get("bundles") or []
        }
        if unknown:
            raise BundleError(f"selection names no requirement: {sorted(unknown)}")
        if missing:
            raise BundleNotFoundError("; ".join(missing + failed))
        if failed:
            raise BundleVerificationError("; ".join(failed))
        return resolved

    def _select(self, req, pinned, satisfies) -> BundleRecord | None:
        if pinned is not None:
            rec = self.store.get(*pinned)
            if (
                rec is None
                or rec.status != "active"
                or rec.bundle_type != req["type"]
                or (req.get("id") and rec.bundle_id != req["id"])
                or (req.get("version") and not satisfies(rec.version, req["version"]))
            ):
                return None
            return rec
        candidates = [
            r
            for r in self.store.list()
            if r.status == "active"
            and r.bundle_type == req["type"]
            and (not req.get("id") or r.bundle_id == req["id"])
            and (not req.get("version") or satisfies(r.version, req["version"]))
        ]
        if not candidates:
            return None
        # Prefer a verified version; a failed one is only returned if nothing else
        # fits, so that the caller gets a verification error rather than "missing".
        candidates.sort(
            key=lambda r: (r.verification_status == "verified", _version_key(r.version))
        )
        return candidates[-1]


def _version_key(version: str):
    return _contracts().parse_semver(version)


# ------------------------------------------------------------------------------- CLI


def _make_registry(args: argparse.Namespace) -> tuple[BundleRegistry, Any]:
    root = args.root or os.environ.get("BUNDLE_ROOT")
    if not root:
        raise SystemExit("error: give --root or set BUNDLE_ROOT")
    if args.store == "db":
        from app.db.session import SessionLocal

        session = SessionLocal()
        return BundleRegistry(root, SqlAlchemyStore(session)), session
    return BundleRegistry(root), None


def _print_table(records: Iterable[BundleRecord]) -> None:
    rows = [
        (
            r.bundle_id,
            r.version,
            r.bundle_type,
            r.status,
            r.verification_status,
            "yes" if r.synthetic else "no",
            r.licence_spdx,
        )
        for r in records
    ]
    header = ("ID", "VERSION", "TYPE", "STATUS", "VERIFICATION", "SYNTHETIC", "LICENCE")
    widths = [max(len(str(x[i])) for x in [header, *rows]) for i in range(len(header))]
    for row in [header, *rows]:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)).rstrip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bundles", description="Reference bundle registry (contract bundles 1.x)."
    )
    parser.add_argument("--root", help="bundle root (default: $BUNDLE_ROOT)")
    parser.add_argument(
        "--store",
        choices=["json", "db"],
        default="json",
        help="registry rows in <root>/.registry.json (default) or the database",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("register", help="verify and register a bundle directory")
    p.add_argument("path")
    p = sub.add_parser("verify", help="re-verify registered bundles (full re-hash)")
    p.add_argument("bundle_id", nargs="?")
    p.add_argument("version", nargs="?")
    p.add_argument("--quick", action="store_true", help="skip hashing (sizes only)")
    p = sub.add_parser("list", help="list registered bundles")
    p.add_argument("--type")
    p.add_argument("--status", choices=["active", "withdrawn"])
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("withdraw", help="withdraw a bundle from new runs")
    p.add_argument("bundle_id")
    p.add_argument("version")
    p = sub.add_parser("resolve", help="resolve the bundles a step.yaml requires")
    p.add_argument("step_yaml")
    args = parser.parse_args(argv)

    registry, session = _make_registry(args)
    try:
        if args.cmd == "register":
            rec = registry.register(args.path)
            print(f"registered {rec.bundle_id} {rec.version} ({rec.content_digest})")
        elif args.cmd == "verify":
            if args.bundle_id:
                if not args.version:
                    parser.error("verify needs both bundle_id and version, or neither")
                registry.verify(args.bundle_id, args.version, full=not args.quick)
                print(f"ok {args.bundle_id} {args.version}")
            else:
                bad = 0
                for rec, err in registry.verify_all(full=not args.quick):
                    print(
                        ("FAILED " if err else "ok ")
                        + f"{rec.bundle_id} {rec.version}"
                        + (f": {err}" if err else "")
                    )
                    bad += bool(err)
                return 1 if bad else 0
        elif args.cmd == "list":
            recs = registry.list(args.type, args.status)
            if args.json:
                print(json.dumps([r.to_dict() for r in recs], indent=2))
            else:
                _print_table(recs)
        elif args.cmd == "withdraw":
            registry.withdraw(args.bundle_id, args.version)
            print(f"withdrawn {args.bundle_id} {args.version}")
        elif args.cmd == "resolve":
            manifest = yaml.safe_load(Path(args.step_yaml).read_text(encoding="utf-8"))
            for r in registry.resolve(manifest).values():
                print(
                    f"{r.name}: {r.bundle_id} {r.version} {r.host_path} -> {r.mount_path}"
                )
    except BundleError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if session is not None:
            session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
