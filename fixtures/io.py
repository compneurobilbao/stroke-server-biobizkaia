"""Deterministic file writers (no timestamps, no host-dependent metadata)."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import zipfile
from pathlib import Path

import nibabel as nib
import numpy as np

# MNI152NLin6Asym 2 mm working grid (mirrors contracts/artefact-types.yaml;
# tests/test_fixtures.py checks that the two agree).
MNI2_SHAPE = (91, 109, 91)
MNI2_AFFINE = np.array(
    [[-2, 0, 0, 90], [0, 2, 0, -126], [0, 0, 2, -72], [0, 0, 0, 1]], dtype=np.float64
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def nifti_bytes(data: np.ndarray, affine: np.ndarray, *, mni: bool) -> bytes:
    """Serialise a NIfTI-1 image to uncompressed bytes."""
    img = nib.Nifti1Image(data, affine)
    img.header.set_data_dtype(data.dtype)
    code = 4 if mni else 1  # 4 = MNI152, 1 = scanner
    img.set_sform(affine, code=code)
    img.set_qform(affine, code=code)
    return img.to_bytes()


def write_nifti_gz(
    path: Path, data: np.ndarray, affine: np.ndarray, *, mni: bool
) -> None:
    """Write a gzip NIfTI with a zeroed gzip mtime and no stored file name."""
    raw = nifti_bytes(data, affine, mni=mni)
    buf = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=buf, mtime=0, compresslevel=9
    ) as gz:
        gz.write(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(buf.getvalue())


def payload_sha256(path: Path) -> str:
    """SHA-256 of the file content as stored (decompressed for ``.gz``).

    gzip output can differ between zlib builds, so cross-platform
    reproducibility is asserted on the decompressed payload.
    """
    raw = Path(path).read_bytes()
    if str(path).endswith(".gz"):
        raw = gzip.decompress(raw)
    return sha256_bytes(raw)


def write_npz(path: Path, **arrays: np.ndarray) -> None:
    """Write an uncompressed ``.npz`` with fixed member timestamps."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        for name in sorted(arrays):
            buf = io.BytesIO()
            np.lib.format.write_array(
                buf, np.ascontiguousarray(arrays[name]), allow_pickle=False
            )
            info = zipfile.ZipInfo(name + ".npy", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3  # ZipInfo defaults to 0 on Windows, 3 elsewhere
            info.external_attr = 0o644 << 16
            zf.writestr(info, buf.getvalue())


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))  # LF endings on every platform


def write_json(path: Path, obj: object) -> None:
    write_text(path, json.dumps(obj, indent=2, sort_keys=True) + "\n")
