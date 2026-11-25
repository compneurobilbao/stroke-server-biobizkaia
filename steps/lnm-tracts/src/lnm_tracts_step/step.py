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

Since 1.1.0 the atlas may declare the space of its streamlines (``metadata.space`` of its
``bundle.yaml``). When that space is not the lesion's (MNI152NLin6Asym), the lesion is
first resampled into the atlas space with the optional ``space_transform`` bundle
(``space.py``); otherwise nothing changes. An atlas without ``metadata.space`` is taken
to be in MNI152NLin6Asym, as in 1.0.0.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import nibabel as nib
import numpy as np

from stroke_step_sdk import InputRejected, InternalFailure, StepContext

from . import space

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


def exclusion_mask(ctx: StepContext, lesion: Path, atlas: Path, scratch: Path) -> Path:
    """The mask the streamlines are tested against: the lesion itself when the atlas is in
    the lesion space, else the lesion resampled into the atlas space (``space.py``)."""
    atlas_space = space.atlas_space(space.read_bundle_doc(atlas))
    mounted = space.TRANSFORM_BUNDLE in ctx.bundles
    if atlas_space == space.LESION_SPACE:
        if mounted:
            ctx.log.info(
                "atlas is in the lesion space (%s): the space-transform bundle is not used",
                atlas_space,
            )
        return lesion
    if not mounted:
        raise InternalFailure(
            "space_transform_missing",
            "The tract atlas is not in the lesion space and no space-transform bundle is mounted.",
            details={"lesion_space": space.LESION_SPACE, "atlas_space": atlas_space},
        )
    transform = space.load_transform(
        ctx.bundle_path(space.TRANSFORM_BUNDLE), space.LESION_SPACE, atlas_space
    )
    result = space.lesion_to_atlas_space(
        lesion, transform, scratch / "lesion_mask_atlas_space.nii.gz"
    )
    record = {
        "lesion_space": space.LESION_SPACE,
        "atlas_space": atlas_space,
        "transform_bundle": {"id": transform.bundle_id, "version": transform.version},
        "method": space.METHOD,
        "target_grid_shape": list(transform.coords.shape[:3]),
        **result.summary(),
    }
    ctx.log.info("lesion resampled into the atlas space: %s", json.dumps(record))
    change = (
        100.0 * (result.volume_after_mm3 - result.volume_before_mm3) / result.volume_before_mm3
    )
    ctx.warn(
        "lesion_resampled_to_atlas_space",
        f"Lesion mask resampled from {space.LESION_SPACE} to the atlas space {atlas_space} "
        f"with the space-transform bundle {transform.bundle_id} {transform.version} "
        f"({space.METHOD}) before tract disconnection: {result.voxels_before} voxels, "
        f"{result.volume_before_mm3:.1f} mm3 before; {result.voxels_after} voxels, "
        f"{result.volume_after_mm3:.1f} mm3 after ({change:+.1f}%).",
        port="lesion_mask",
    )
    ctx.extra_path("atlas_space_resampling.json").write_text(
        json.dumps(record, indent=1) + "\n", encoding="utf-8"
    )
    shutil.copyfile(result.path, ctx.extra_path("lesion_mask_atlas_space.nii.gz"))
    if result.voxels_after == 0:
        raise InputRejected(
            "mask_outside_atlas_space",
            "The lesion mask has no voxel left after resampling into the atlas space.",
            port="lesion_mask",
        )
    return result.path


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
        mask = exclusion_mask(ctx, lesion, atlas, scratch)
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
            exclude_lesion(ctx, source, mask, kept)
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
