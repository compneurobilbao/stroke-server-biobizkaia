"""Tract-wise structural disconnection, ported from ``src/lnm_tracts.sh``.

The algorithm is the script's: for every tract of the atlas, count the streamlines
(``tckstats -output count``), remove those that pass through the lesion
(``tckedit -exclude``), count again. The script reports the surviving fraction (its
``Disc`` column); this step reports the removed fraction, ``1 - Disc``, as the contract's
``tract_disconnection``, computed from the integer counts rather than from bc's
three-digit truncated ratio.

Differences from the script, none of which changes a result: per-case scratch in ``/tmp``
(the script wrote into the shared data tree), exit codes of every MRtrix call are checked,
and tract names are validated before they become file names.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import nibabel as nib
import numpy as np

from stroke_step_sdk import InputRejected, InternalFailure, StepContext

BUNDLE = "tract_atlas"
LABELS_FILE = "labels.txt"
TRACT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$")
VERSION_RE = re.compile(r"==\s*\S+\s+([0-9][^\s=]*)\s*==")
HEADER = ("tract", "disconnection", "n_streamlines", "n_disconnected")


def read_labels(bundle_dir: Path) -> list[str]:
    """Tract names of the atlas bundle, in file order; each must have ``<name>.tck``."""
    labels_path = bundle_dir / LABELS_FILE
    if not labels_path.is_file():
        raise InternalFailure(
            "bundle_invalid", "The tract atlas bundle has no labels.txt."
        )
    names = [
        line.strip()
        for line in labels_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not names:
        raise InternalFailure("bundle_invalid", "The tract atlas lists no tracts.")
    if len(set(names)) != len(names):
        raise InternalFailure(
            "bundle_invalid", "The tract atlas lists a tract name twice."
        )
    for name in names:
        if not TRACT_RE.match(name):
            raise InternalFailure(
                "bundle_invalid", "The tract atlas has an unsupported tract name."
            )
        if not (bundle_dir / f"{name}.tck").is_file():
            raise InternalFailure(
                "bundle_invalid",
                "The tract atlas lists a tract without a streamline file.",
                details={"tract": name},
            )
    return names


def disconnection(n_total: int, n_surviving: int) -> float:
    """Fraction of streamlines removed by the lesion (``1 - Disc`` of the old script)."""
    return (n_total - n_surviving) / n_total


def _tool(ctx: StepContext, argv: list[str]) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL
        )
    except OSError as exc:
        raise InternalFailure(
            "mrtrix_unavailable",
            "An MRtrix3 command could not be started.",
            details={"command": Path(argv[0]).name},
        ) from exc
    if proc.returncode != 0:
        ctx.log.error("%s failed: %s", Path(argv[0]).name, proc.stderr.strip()[-500:])
    return proc


def _header_count(tck: Path) -> int | None:
    """``count`` field of a .tck header (text up to the END line), or None."""
    with open(tck, "rb") as fh:
        head = fh.read(4096).split(b"\nEND\n", 1)[0].decode("ascii", "replace")
    m = re.search(r"^count:\s*(\d+)\s*$", head, re.M)
    return int(m.group(1)) if m else None


def count_streamlines(ctx: StepContext, tck: Path) -> int:
    proc = _tool(ctx, ["tckstats", "-quiet", "-output", "count", str(tck)])
    if proc.returncode == 0:
        try:
            return int(proc.stdout.strip().split()[-1])
        except (IndexError, ValueError):
            pass
    # tckstats may refuse an empty tractogram; the header of what tckedit wrote is exact.
    if _header_count(tck) == 0:
        return 0
    raise InternalFailure(
        "mrtrix_failed",
        "An MRtrix3 command failed.",
        details={"command": "tckstats", "exit_code": proc.returncode},
    )


def exclude_lesion(ctx: StepContext, tck: Path, lesion: Path, out: Path) -> None:
    cpus = str(ctx.cpus)
    proc = _tool(
        ctx,
        ["tckedit", "-quiet", "-force", "-nthreads", cpus, str(tck),
         "-exclude", str(lesion), str(out)],
    )  # fmt: skip
    if proc.returncode != 0 or not out.is_file():
        raise InternalFailure(
            "mrtrix_failed",
            "An MRtrix3 command failed.",
            details={"command": "tckedit", "exit_code": proc.returncode},
        )


def tool_version(argv0: str) -> str | None:
    try:
        proc = subprocess.run(
            [argv0, "-version"], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    m = VERSION_RE.search(proc.stdout + proc.stderr)
    return m.group(1) if m else None


def run(ctx: StepContext) -> None:
    lesion = ctx.input_path("lesion_mask")
    if not np.any(np.asanyarray(nib.load(str(lesion)).dataobj)):
        raise InputRejected(
            "mask_empty", "The lesion mask has no voxels set.", port="lesion_mask"
        )

    atlas = ctx.bundle_path(BUNDLE)
    tracts = read_labels(atlas)
    ctx.log.info("%d tracts in the atlas", len(tracts))

    rows = []
    scratch = Path(tempfile.mkdtemp(prefix="lnm-tracts-", dir=os.environ.get("TMPDIR")))
    try:
        for name in tracts:
            source = atlas / f"{name}.tck"
            n_total = count_streamlines(ctx, source)
            if n_total == 0:
                raise InternalFailure(
                    "bundle_invalid",
                    "A tract of the atlas has no streamlines.",
                    details={"tract": name},
                )
            kept = scratch / f"{name}.tck"
            exclude_lesion(ctx, source, lesion, kept)
            n_surviving = count_streamlines(ctx, kept)
            kept.unlink()
            n_cut = n_total - n_surviving
            if not 0 <= n_cut <= n_total:
                raise InternalFailure(
                    "mrtrix_failed", "An MRtrix3 command returned an impossible count."
                )
            ctx.log.info("%s: %d of %d streamlines disconnected", name, n_cut, n_total)
            rows.append((name, disconnection(n_total, n_surviving), n_total, n_cut))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    lines = ["\t".join(HEADER)]
    lines += [f"{n}\t{d:.6f}\t{t}\t{c}" for n, d, t, c in rows]
    ctx.output_path("tract_disconnection").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )

    version = tool_version("tckedit")
    if version:
        ctx.add_software("mrtrix3", version)
    ctx.add_software("nibabel", nib.__version__)
    ctx.add_software("numpy", np.__version__)
