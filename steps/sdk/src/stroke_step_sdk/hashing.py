"""File hashing for provenance records."""

from __future__ import annotations

import hashlib
from pathlib import Path


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def file_record(port: str, type_name: str, path: Path) -> dict:
    return {
        "port": port,
        "type": type_name,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
    }
